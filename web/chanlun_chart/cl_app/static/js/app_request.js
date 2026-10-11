// 同源请求统一处理 CSRF 生命周期和认证失效。CSRF 失败只在响应明确
// 指向 token 问题时重试一次，普通业务 400 不会被重放。
(function () {
    'use strict';
    if (window.AppRequest && window.AppRequest.requestLayerInstalled) return;
    var meta = document.querySelector('meta[name="csrf-token"]');
    var originalFetch = typeof window.fetch === 'function' ? window.fetch : null;
    var refreshPromise = null;
    var redirectingToLogin = false;
    var tokenUpdatedAt = Date.now();
    var TOKEN_REFRESH_AGE_MS = 10 * 60 * 60 * 1000;

    function currentToken() {
        return meta && meta.content ? meta.content : '';
    }
    function updateToken(token) {
        if (!token) { return; }
        if (meta) { meta.content = token; }
        tokenUpdatedAt = Date.now();
        if (typeof document.querySelectorAll === 'function') {
            var fields = document.querySelectorAll('input[name="csrf_token"]');
            for (var i = 0; i < fields.length; i += 1) { fields[i].value = token; }
        }
    }        function unsafe(method) {
        return !/^(GET|HEAD|OPTIONS|TRACE)$/i.test(method || 'GET');
    }
    function sameOrigin(url) {
        try { return new URL(url, window.location.href).origin === window.location.origin; }
        catch (_) { return false; }
    }
    function loginRedirectUrl() {
        var next = (window.location.pathname || '/') + (window.location.search || '');
        return '/login?next=' + encodeURIComponent(next);
    }
    function responseUrl(value) {
        return String((value && (value.responseURL || value.url)) || '');
    }
    function isLoginResponse(value) {
        if (!value) { return false; }
        if (value.status === 401) { return true; }
        try {
            var path = new URL(responseUrl(value), window.location.href).pathname;
            return path === '/login' && window.location.pathname !== '/login';
        } catch (_) { return false; }
    }        function handleAuthenticationFailure(value) {
        if (!isLoginResponse(value) || redirectingToLogin) { return false; }
        redirectingToLogin = true;
        if (window.location.pathname !== '/login') {
            window.location.assign(loginRedirectUrl());
        }
        return true;
    }
    function isCsrfFailureText(text) {
        var body = String(text || '');
        try { return JSON.parse(body).code === 'csrf_failed'; }
        catch (_) { return /\bCSRF(?: session)? tokens? (?:is missing|is invalid|has expired|do not match)\b/i.test(body); }
    }
    function isCsrfFailureResponse(response) {
        if (!response || response.status !== 400 || typeof response.clone !== 'function') {
            return Promise.resolve(false);
        }
        return response.clone().text().then(isCsrfFailureText).catch(function () { return false; });
    }
    function fetchWithTimeout(input, init) {
        var requestInit = Object.assign({}, init || {});
        if (requestInit.signal || typeof AbortController === 'undefined') {
            return originalFetch.call(window, input, requestInit);
        }
        var controller = new AbortController();
        requestInit.signal = controller.signal;
        var timeoutId = setTimeout(function () { controller.abort(); }, 30000);
        return originalFetch.call(window, input, requestInit).finally(function () {
            clearTimeout(timeoutId);
        });
    }
    function abortReason(signal) {
        if (signal && signal.reason !== undefined) return signal.reason;
        var error = new Error('request aborted'); error.name = 'AbortError'; return error;
    }
    function waitForRefresh(promise, signal) {
        if (!signal) return promise;
        if (signal.aborted) { promise.catch(function () {}); return Promise.reject(abortReason(signal)); }
        // One caller's deadline must not cancel the token refresh other callers
        // are waiting for, or allow that caller's POST to restart afterwards.
        return new Promise(function (resolve, reject) {
            function aborted() { signal.removeEventListener('abort', aborted); reject(abortReason(signal)); }
            signal.addEventListener('abort', aborted, { once: true });
            promise.then(function (value) {
                signal.removeEventListener('abort', aborted); resolve(value);
            }, function (error) { signal.removeEventListener('abort', aborted); reject(error); });
        });
    }
    function refreshCsrfToken() {
        if (refreshPromise) { return refreshPromise; }
        if (!originalFetch) { return Promise.reject(new Error('fetch is unavailable')); }
        refreshPromise = fetchWithTimeout('/api/session', {
            method: 'GET',
            credentials: 'same-origin',
            cache: 'no-store',
            headers: { 'Accept': 'application/json' }
        }).then(function (response) {
            if (handleAuthenticationFailure(response)) {
                throw new Error('authentication required');
            }
            if (!response.ok) {
                throw new Error('CSRF refresh failed with HTTP ' + response.status);
            }
            return response.json();
        }).then(function (data) {
            var token = data && data.csrf_token;
            if (!token) { throw new Error('CSRF refresh returned no token'); }
            updateToken(token);
            return token;
        }).finally(function () {
            refreshPromise = null;
        });
        return refreshPromise;
    }
    function requestMethod(input, init) {
        return (init && init.method) || (input && input.method) || 'GET';
    }
    function requestUrl(input) {
        return typeof input === 'string' ? input : (input && input.url) || String(input || '');
    }
    function prepareFetchInit(input, init) {
        var requestInit = Object.assign({}, init || {});
        var method = requestMethod(input, requestInit);
        var url = requestUrl(input);
        if (unsafe(method) && sameOrigin(url)) {
            var headers = new Headers(requestInit.headers || (input && input.headers) || {});
            headers.set('X-CSRFToken', currentToken());
            requestInit.headers = headers;
        }
        requestInit.credentials = requestInit.credentials || 'same-origin';
        if (!requestInit.signal && input && input.signal) requestInit.signal = input.signal;
        return requestInit;
    }
    function guardedFetch(input, init, retried) {
        var requestInit = prepareFetchInit(input, init);
        var method = requestMethod(input, requestInit);
        var url = requestUrl(input);
        if (requestInit.signal && requestInit.signal.aborted) return Promise.reject(abortReason(requestInit.signal));
        var sentToken = currentToken();
        var retryInput = !retried && unsafe(method) && sameOrigin(url) && input && typeof input.clone === 'function'
            ? input.clone() : input;
        return fetchWithTimeout(input, requestInit).then(function (response) {
            if (sameOrigin(url)) handleAuthenticationFailure(response);
            if (retried || !unsafe(method) || !sameOrigin(url)) { return response; }
            return isCsrfFailureResponse(response).then(function (isCsrfFailure) {
                if (!isCsrfFailure) { return response; }
                if (requestInit.signal && requestInit.signal.aborted) throw abortReason(requestInit.signal);
                var refresh = sentToken === currentToken() ? refreshCsrfToken() : Promise.resolve(currentToken());
                return waitForRefresh(refresh, requestInit.signal).then(function () {
                    return guardedFetch(retryInput, requestInit, true);
                });
            });
        });
    }

    if (originalFetch) {
        window.fetch = function (input, init) {
            var options = Object.assign({}, init || {});
            if (!options.signal && input && input.signal) options.signal = input.signal;
            if (options.signal || typeof AbortController === 'undefined') return guardedFetch(input, options, false);
            var controller = new AbortController(); options.signal = controller.signal;
            var timeout = setTimeout(function () { controller.abort(); }, 30000);
            return guardedFetch(input, options, false).finally(function () { clearTimeout(timeout); });
        };
    }

    var jq = window.jQuery;
    var nativeAjax = jq && jq.ajax;
    if (jq) {
        jq.ajaxSetup({
            beforeSend: function (xhr, settings) {
                if (settings.crossDomain || !unsafe(settings.type)) { return; }
                xhr.setRequestHeader('X-CSRFToken', currentToken());
            }
        });
    }

    function isCsrfXhr(xhr) {
        return !!xhr && xhr.status === 400 && isCsrfFailureText(xhr.responseText);
    }
    function appAjax(options) {
        if (!nativeAjax) throw new Error('jQuery AJAX is unavailable on this page');
        if (!jq.Deferred) { return nativeAjax.call(jq, options); }
        var settings = Object.assign({}, options || {});
        var deferred = jq.Deferred();
        var activeRequest = null;
        var aborted = false;
        var retried = false;
        var settled = false;
        var context = settings.context || settings;

        function complete(args) {
            if (typeof settings.complete !== 'function') { return; }
            try { settings.complete.apply(context, args); }
            catch (error) { console.error('[AppRequest] complete callback failed', error); }
        }
        function reject(args) {
            if (settled) { return; }
            settled = true;
            handleAuthenticationFailure(args[0]);
            if (typeof settings.error === 'function') {
                try { settings.error.apply(context, args); }
                catch (error) { console.error('[AppRequest] error callback failed', error); }
            }
            try { deferred.rejectWith(context, args); }
            catch (error) { console.error('[AppRequest] fail handler failed', error); }
            finally { complete([args[0], args[1]]); }
        }
        function issue() {
            if (aborted) { return; }
            var sentToken = currentToken();
            var attemptOptions = Object.assign({}, settings);
            delete attemptOptions.success;
            delete attemptOptions.error;
            delete attemptOptions.complete;
            activeRequest = nativeAjax.call(jq, attemptOptions);
            activeRequest.done(function () {
                var args = Array.prototype.slice.call(arguments);
                if (settled) { return; }
                if (handleAuthenticationFailure(args[2])) {
                    reject([args[2], 'authentication_required', new Error('authentication required')]);
                    return;
                }
                settled = true;
                if (typeof settings.success === 'function') {
                    try { settings.success.apply(context, args); }
                    catch (error) { console.error('[AppRequest] success callback failed', error); }
                }
                try { deferred.resolveWith(context, args); }
                catch (error) { console.error('[AppRequest] done handler failed', error); }
                finally { complete([args[2], args[1]]); }
            });                activeRequest.fail(function () {
                var args = Array.prototype.slice.call(arguments);
                var xhr = args[0];
                if (!retried && unsafe(settings.type || settings.method) && sameOrigin(settings.url || window.location.href) && isCsrfXhr(xhr)) {
                    retried = true;
                    (sentToken === currentToken() ? refreshCsrfToken() : Promise.resolve()).then(issue).catch(function () { reject(args); });
                    return;
                }
                reject(args);
            });
        }

        var promise = deferred.promise();
        promise.abort = function () {
            if (aborted || settled) { return promise; }
            aborted = true;
            if (activeRequest && typeof activeRequest.abort === 'function') {
                activeRequest.abort();
            }
            if (!settled) {
                reject([activeRequest, 'abort', new Error('request aborted')]);
            }
            return promise;
        };
        issue();
        return promise;
    }

    // 原生 XHR（包括第三方控件）也动态注入最新 token，并识别认证失效。
    if (window.XMLHttpRequest && window.XMLHttpRequest.prototype) {
        var originalOpen = window.XMLHttpRequest.prototype.open;
        var originalSend = window.XMLHttpRequest.prototype.send;
        var originalSetRequestHeader = window.XMLHttpRequest.prototype.setRequestHeader;
        window.XMLHttpRequest.prototype.open = function (method, url) {
            this.__clCsrfMethod = method;
            this.__clCsrfUrl = url;
            this.__clRequestHeaders = {};
            return originalOpen.apply(this, arguments);
        };
        window.XMLHttpRequest.prototype.setRequestHeader = function (name, value) {
            this.__clRequestHeaders = this.__clRequestHeaders || {};
            this.__clRequestHeaders[String(name).toLowerCase()] = value;
            return originalSetRequestHeader.call(this, name, value);
        };
        window.XMLHttpRequest.prototype.send = function () {
            var hasToken = this.__clRequestHeaders && this.__clRequestHeaders['x-csrftoken'];
            if (!hasToken && unsafe(this.__clCsrfMethod) && sameOrigin(this.__clCsrfUrl)) {
                this.setRequestHeader('X-CSRFToken', currentToken());
            }
            if (typeof this.addEventListener === 'function') {
                this.addEventListener('loadend', function () {
                    if (!sameOrigin(this.__clCsrfUrl)) return;
                    handleAuthenticationFailure(this);
                    if (isCsrfXhr(this) && this.__clRequestHeaders['x-csrftoken'] === currentToken()) { refreshCsrfToken().catch(function () {}); }
                }, { once: true });
            }
            return originalSend.apply(this, arguments);
        };
    }

    if (typeof document.addEventListener === 'function') {
        document.addEventListener('submit', function (event) {
            if (event.defaultPrevented) return;
            var form = event.target;
            // An anonymous login must remain a normal POST; /api/session requires login.
            if (window.location.pathname === '/login') return;
            if (!form || Date.now() - tokenUpdatedAt < TOKEN_REFRESH_AGE_MS) { return; }
            var method = String(form.method || 'GET').toUpperCase();
            var action = form.action || window.location.href;
            if (!unsafe(method) || !sameOrigin(action)) { return; }
            event.preventDefault();
            refreshCsrfToken().then(function () {
                if (typeof form.submit === 'function') { form.submit(); }
            }).catch(function () {});
        });
    }
    window.AppRequest = {
        requestLayerInstalled: true,
        ajax: appAjax,
        fetch: window.fetch,
        refreshCsrfToken: refreshCsrfToken,
        handleAuthenticationFailure: handleAuthenticationFailure
    };
})();
