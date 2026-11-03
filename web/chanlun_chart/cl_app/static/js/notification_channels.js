/* Private notification destinations; never put credentials in browser storage. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const form = $('notification-channels-form');
  if (!form) return;
  const channels = ['holdings', 'others'];
  const field = (channel, name) => $(`notification-channel-${channel}-${name}`);
  let saved = null, busy = false;
  function draft() {
    const result = {};
    for (const channel of channels) {
      const changes = {};
      for (const name of ['webhook', 'secret']) {
        const value = field(channel, name).value.trim();
        if (field(channel, 'clear-' + name).checked) changes[name] = '';
        else if (value) changes[name] = value;
      }
      const keyword = field(channel, 'keyword').value.trim();
      if (keyword !== saved?.[channel]?.keyword) changes.keyword = keyword;
      if (Object.keys(changes).length) result[channel] = changes;
    }
    return result;
  }
  function controls() {
    $('notification-channels-fields').disabled = busy || !saved;
    $('notification-channels-save').disabled = busy || !saved || !Object.keys(draft()).length;
    $('notification-channels-save').textContent = busy ? '正在保存…' : '保存通知地址';
    for (const channel of channels) for (const name of ['webhook', 'secret']) {
      field(channel, name).disabled = field(channel, 'clear-' + name).checked;
    }
  }
  function apply(value) {
    if (!value || channels.some(channel => !value[channel] || typeof value[channel].configured !== 'boolean'
        || typeof value[channel].keyword !== 'string')) throw Error('通知地址状态暂不可读取');
    saved = value;
    for (const channel of channels) {
      const status = value[channel];
      field(channel, 'status').textContent = status.configured
        ? `已配置 · ${status.webhook_masked} · ${status.secret_configured ? '已设置加签' : '未设置加签'}`
        : status.configuration_error || '尚未配置地址';
      field(channel, 'keyword').value = status.keyword;
      for (const name of ['webhook', 'secret']) {
        field(channel, name).value = '';
        field(channel, 'clear-' + name).checked = false;
      }
      field(channel, 'webhook').placeholder = status.configured ? '已配置；输入新地址可替换，留空保持原值' : '粘贴完整的钉钉机器人地址';
      field(channel, 'secret').placeholder = status.secret_configured ? '已设置加签；输入新密钥可替换，留空保持原值' : '机器人启用加签时填写';
    }
  }
  async function request(body) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 15000);
    const options = {credentials: 'same-origin', signal: controller.signal, headers: {Accept: 'application/json'}};
    if (body !== undefined) Object.assign(options, {method: 'POST', body: JSON.stringify(body),
      headers: {...options.headers, 'Content-Type': 'application/json',
        'X-CSRFToken': document.querySelector('meta[name=csrf-token]').content}});
    try {
      const response = await fetch('/monitor/notification-channels', options);
      if (response.redirected || response.status === 401) throw Error('登录已过期，请重新登录');
      const result = await response.json();
      if (!response.ok) throw Error(result.error || '通知地址请求失败');
      return result.channels;
    } catch (error) {
      if (error.name === 'AbortError') throw Error('请求超时，请重试');
      throw error;
    } finally { clearTimeout(timer); }
  }
  async function load() {
    if (busy) return;
    busy = true; controls();
    $('notification-channels-reload').hidden = true;
    try {
      apply(await request());
      $('notification-channels-message').textContent = '两组通知独立发送；可在此修改地址、加签密钥和关键词。';
    } catch (error) {
      $('notification-channels-message').textContent = '读取失败：' + error.message;
      $('notification-channels-reload').hidden = false;
    } finally { busy = false; controls(); }
  }
  for (const channel of channels) for (const name of ['webhook', 'secret', 'keyword', 'clear-webhook', 'clear-secret']) {
    field(channel, name).addEventListener(name.startsWith('clear-') ? 'change' : 'input', () => {
      controls();
      $('notification-channels-message').textContent = Object.keys(draft()).length ? '有未保存的通知地址设置。' : '';
    });
  }
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (busy || !saved) return;
    const body = draft();
    if (!Object.keys(body).length) return;
    busy = true; controls();
    $('notification-channels-message').textContent = '正在保存通知地址…';
    try {
      apply(await request(body));
      $('notification-channels-message').textContent = '通知地址已保存，后续发送与重试按两组地址独立推送。';
      document.dispatchEvent(new CustomEvent('notification-channels-saved'));
    } catch (error) {
      $('notification-channels-message').textContent = '保存失败：' + error.message + '。未保存的修改已保留。';
    } finally { busy = false; controls(); }
  });
  $('notification-channels-reload').addEventListener('click', () => void load());
  void load();
})();
