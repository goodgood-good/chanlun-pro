(function (window, document) {
  "use strict";

  if (!document || typeof document.querySelectorAll !== "function") return;
  if (!window.layui) return;

  var idCounter = 0;
  var syncScheduled = false;
  var activeModal = null;
  var modalBackgroundState = [];
  var lastInteractionTarget = null;
  var pendingDropdownFocus = null;
  var fallbackDropdownTriggers = [];

  function cleanText(value) {
    return String(value || "").replace(/\s+/g, " ").trim();
  }

  function labelledByText(source) {
    var ids = cleanText(source.getAttribute("aria-labelledby")).split(" ").filter(Boolean);
    return ids.map(function (id) {
      var node = document.getElementById(id);
      return node ? cleanText(node.textContent) : "";
    }).filter(Boolean).join(" ");
  }

  function explicitLabelText(source) {
    if (!source.id) return "";
    var labels = document.querySelectorAll("label[for]");
    for (var index = 0; index < labels.length; index += 1) {
      if (labels[index].htmlFor === source.id) return cleanText(labels[index].textContent);
    }
    return "";
  }

  function nearbyLabelText(source) {
    var wrappingLabel = source.closest && source.closest("label");
    if (wrappingLabel) return cleanText(wrappingLabel.textContent);
    var field = source.closest && source.closest(
      ".layui-form-item, .cp-field, .zx-manager-transfer-select",
    );
    if (!field) return "";
    var label = field.querySelector(
      ".layui-form-label, .cp-field-label, .zx-field-label, label",
    );
    return label ? cleanText(label.textContent) : "";
  }

  function accessibleLabel(source, fallback) {
    return cleanText(source.getAttribute("aria-label"))
      || labelledByText(source)
      || explicitLabelText(source)
      || cleanText(source.getAttribute("title"))
      || nearbyLabelText(source)
      || cleanText(source.name)
      || fallback;
  }

  function sourceFor(widget, selector) {
    var source = widget && widget.previousElementSibling;
    return source && source.matches(selector) ? source : null;
  }

  function ensureId(node, prefix) {
    if (!node.id) {
      idCounter += 1;
      node.id = prefix + "-" + idCounter;
    }
    return node.id;
  }

  function syncSelect(widget) {
    var source = sourceFor(widget, "select");
    var input = widget.querySelector(".layui-select-title input");
    var listbox = widget.querySelector("dl");
    if (!source || !input || !listbox) return;

    var label = accessibleLabel(source, "选择选项");
    var listboxId = ensureId(listbox, "layui-listbox");
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-label", label);
    input.setAttribute("aria-autocomplete", "none");
    input.setAttribute("autocomplete", "off");
    input.setAttribute("aria-controls", listboxId);
    input.setAttribute(
      "aria-expanded",
      widget.classList.contains("layui-form-selected") ? "true" : "false",
    );
    input.setAttribute("aria-disabled", source.disabled ? "true" : "false");
    if (source.disabled) input.setAttribute("tabindex", "-1");
    else input.removeAttribute("tabindex");
    widget.querySelectorAll(".layui-edge").forEach(function (edge) {
      edge.setAttribute("aria-hidden", "true");
    });

    listbox.setAttribute("role", "listbox");
    listbox.setAttribute("aria-label", label);
    var selectedId = "";
    listbox.querySelectorAll("dd").forEach(function (option, index) {
      if (!option.id) option.id = listboxId + "-option-" + index;
      var selected = option.classList.contains("layui-this");
      option.setAttribute("role", "option");
      option.setAttribute("aria-selected", selected ? "true" : "false");
      option.setAttribute(
        "aria-disabled",
        option.classList.contains("layui-disabled") ? "true" : "false",
      );
      if (selected) selectedId = option.id;
    });
    var activeId = widget.__cpA11yActiveOptionId;
    var activeOption = activeId && document.getElementById(activeId);
    if (!widget.classList.contains("layui-form-selected")
        || !activeOption || !listbox.contains(activeOption)) {
      activeId = selectedId;
      widget.__cpA11yActiveOptionId = activeId;
    }
    if (activeId) input.setAttribute("aria-activedescendant", activeId);
    else input.removeAttribute("aria-activedescendant");
  }

  function selectOptions(widget) {
    return Array.prototype.filter.call(widget.querySelectorAll("dl > dd"), function (option) {
      return !option.classList.contains("layui-disabled")
        && option.getAttribute("aria-disabled") !== "true";
    });
  }

  function setSelectActive(widget, input, option) {
    widget.querySelectorAll("dl > dd.cp-option-active").forEach(function (node) {
      node.classList.remove("cp-option-active");
    });
    if (!option) return;
    option.classList.add("cp-option-active");
    widget.__cpA11yActiveOptionId = ensureId(option, "layui-option");
    input.setAttribute("aria-activedescendant", widget.__cpA11yActiveOptionId);
    if (typeof option.scrollIntoView === "function") {
      option.scrollIntoView({ block: "nearest" });
    }
  }

  function openSelect(widget, input, last) {
    if (!widget.classList.contains("layui-form-selected")) input.click();
    window.setTimeout(function () {
      if (!widget.isConnected) return;
      syncSelect(widget);
      var options = selectOptions(widget);
      var active = document.getElementById(widget.__cpA11yActiveOptionId || "");
      if (!active || !widget.contains(active)) active = last ? options[options.length - 1] : options[0];
      setSelectActive(widget, input, active);
    }, 0);
  }

  function closeSelect(widget, input) {
    if (widget.classList.contains("layui-form-selected")) input.click();
    widget.querySelectorAll("dl > dd.cp-option-active").forEach(function (node) {
      node.classList.remove("cp-option-active");
    });
    window.setTimeout(function () {
      if (input.isConnected) {
        syncSelect(widget);
        input.focus();
      }
    }, 0);
  }

  function handleSelectKeys(event) {
    var target = event.target;
    if (!target || !target.matches
        || !target.matches('.layui-form-select .layui-select-title input[role="combobox"]')) {
      return false;
    }
    var widget = target.closest(".layui-form-select");
    var source = sourceFor(widget, "select");
    if (!widget || !source || source.disabled) return false;
    var expanded = widget.classList.contains("layui-form-selected");
    if (event.key === "Tab") {
      if (expanded) {
        if (widget.classList.contains("layui-form-selected")) target.click();
        scheduleSync();
      }
      return false;
    }
    if (event.key === "Escape") {
      if (!expanded) return false;
      event.preventDefault();
      event.stopPropagation();
      closeSelect(widget, target);
      return true;
    }
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      event.stopPropagation();
      if (!expanded) openSelect(widget, target, false);
      else {
        var selected = document.getElementById(target.getAttribute("aria-activedescendant") || "");
        if (selected && widget.contains(selected)) selected.click();
        window.setTimeout(function () {
          if (target.isConnected) {
            syncSelect(widget);
            target.focus();
          }
        }, 0);
      }
      return true;
    }
    if (["ArrowDown", "ArrowUp", "Home", "End"].indexOf(event.key) === -1) return false;
    event.preventDefault();
    event.stopPropagation();
    if (!expanded) {
      openSelect(widget, target, event.key === "ArrowUp" || event.key === "End");
      return true;
    }
    var options = selectOptions(widget);
    var active = document.getElementById(target.getAttribute("aria-activedescendant") || "");
    var index = options.indexOf(active);
    var nextIndex = event.key === "Home" ? 0
      : event.key === "End" ? options.length - 1
      : event.key === "ArrowDown" ? (index + 1) % options.length
      : (index - 1 + options.length) % options.length;
    setSelectActive(widget, target, options[nextIndex]);
    return true;
  }

  function syncChoice(widget) {
    var source = sourceFor(widget, 'input[type="checkbox"], input[type="radio"]');
    if (!source) return;
    var isRadio = source.type === "radio";
    var isSwitch = widget.classList.contains("layui-form-switch");
    widget.setAttribute("role", isRadio ? "radio" : (isSwitch ? "switch" : "checkbox"));
    widget.setAttribute("aria-label", accessibleLabel(source, isRadio ? "单选项" : "复选项"));
    widget.setAttribute("aria-checked", source.checked ? "true" : "false");
    widget.setAttribute("aria-disabled", source.disabled ? "true" : "false");
    widget.tabIndex = source.disabled ? -1 : 0;
  }

  function syncCollapse(title) {
    var content = title.nextElementSibling;
    if (!content || !content.classList.contains("layui-colla-content")) return;
    var item = title.closest(".layui-colla-item");
    var expanded = Boolean(item && item.classList.contains("layui-show"))
      || window.getComputedStyle(content).display !== "none";
    title.setAttribute("role", "button");
    title.setAttribute("tabindex", "0");
    title.setAttribute("aria-controls", ensureId(content, "layui-collapse-panel"));
    title.setAttribute("aria-expanded", expanded ? "true" : "false");
  }

  function syncXmSelect(widget) {
    var popup = widget.querySelector(".xm-body");
    if (!popup) return;
    var container = widget.parentElement;
    var tips = widget.querySelector(".xm-tips");
    var label = cleanText(container && container.getAttribute("aria-label"))
      || cleanText(widget.getAttribute("aria-label"))
      || cleanText(tips && tips.textContent)
      || "搜索标的";
    var listbox = popup.querySelector(".scroll-body") || popup;
    var listboxId = ensureId(listbox, "xm-select-listbox");
    var expanded = !popup.classList.contains("dis");

    widget.setAttribute("role", "combobox");
    widget.setAttribute("aria-label", label);
    widget.setAttribute("aria-haspopup", "listbox");
    widget.setAttribute("aria-controls", listboxId);
    widget.setAttribute("aria-expanded", expanded ? "true" : "false");
    widget.setAttribute("tabindex", "0");
    popup.setAttribute("aria-hidden", expanded ? "false" : "true");
    listbox.setAttribute("role", "listbox");
    listbox.setAttribute("aria-label", label + "搜索结果");

    popup.querySelectorAll('[tabindex]:not(input):not(textarea):not(select)').forEach(function (node) {
      node.setAttribute("tabindex", "-1");
    });
    var search = popup.querySelector(".xm-search-input");
    if (search) {
      search.setAttribute("role", "searchbox");
      search.setAttribute("aria-label", "在" + label + "中输入关键词");
      search.setAttribute("aria-controls", listboxId);
      search.setAttribute("autocomplete", "off");
    }
    listbox.querySelectorAll(".xm-option").forEach(function (option) {
      option.setAttribute("role", "option");
      option.setAttribute(
        "aria-selected",
        option.classList.contains("selected") ? "true" : "false",
      );
    });
  }

  function handleXmSelectKeys(event) {
    var target = event.target;
    var widget = target && target.closest && target.closest("xm-select");
    if (!widget || event.key !== "Escape") return false;
    window.setTimeout(function () {
      var popup = widget.querySelector(".xm-body");
      if (popup && !popup.classList.contains("dis")) widget.click();
      syncXmSelect(widget);
      widget.focus();
    }, 0);
    return true;
  }

  function dropdownTrigger(menu) {
    if (menu && menu.__cpA11yTrigger && menu.__cpA11yTrigger.isConnected) {
      return menu.__cpA11yTrigger;
    }
    var dropdownId = menu && menu.getAttribute("lay-dropdown-id");
    if (!dropdownId) return null;
    var byId = document.getElementById(dropdownId);
    if (byId && byId !== menu) return byId;
    var candidates = document.querySelectorAll("[lay-dropdown-id]");
    for (var index = 0; index < candidates.length; index += 1) {
      if (candidates[index] !== menu
          && !candidates[index].classList.contains("layui-dropdown")
          && candidates[index].getAttribute("lay-dropdown-id") === dropdownId) {
        return candidates[index];
      }
    }
    return null;
  }

  function dropdownForTrigger(trigger) {
    if (!trigger) return null;
    var dropdownId = trigger.getAttribute("lay-dropdown-id") || trigger.id;
    if (!dropdownId) return null;
    var menus = document.querySelectorAll(".layui-dropdown[lay-dropdown-id]");
    for (var index = 0; index < menus.length; index += 1) {
      if (menus[index].getAttribute("lay-dropdown-id") === dropdownId) return menus[index];
    }
    return null;
  }

  function directChildWithClass(parent, className) {
    if (!parent) return null;
    for (var index = 0; index < parent.children.length; index += 1) {
      if (parent.children[index].classList.contains(className)) return parent.children[index];
    }
    return null;
  }

  function directChildByTag(parent, tagName) {
    if (!parent) return null;
    for (var index = 0; index < parent.children.length; index += 1) {
      if (parent.children[index].tagName === tagName) return parent.children[index];
    }
    return null;
  }

  function syncMenuList(list, label, labelledBy) {
    list.setAttribute("role", "menu");
    if (labelledBy) {
      list.setAttribute("aria-labelledby", labelledBy);
      list.removeAttribute("aria-label");
    } else {
      list.setAttribute("aria-label", label);
      list.removeAttribute("aria-labelledby");
    }
    Array.prototype.forEach.call(list.children, function (item) {
      if (item.tagName !== "LI") return;
      if (item.classList.contains("layui-menu-item-divider")) {
        item.setAttribute("role", "separator");
        return;
      }
      item.setAttribute("role", "none");
      var title = directChildWithClass(item, "layui-menu-body-title");
      var isGroup = item.classList.contains("layui-menu-item-group");
      var isEmpty = item.classList.contains("layui-menu-item-none");
      if (title && !isGroup) {
        title.setAttribute("role", "menuitem");
        title.setAttribute("tabindex", "-1");
        if (isEmpty) title.setAttribute("aria-disabled", "true");
        else title.removeAttribute("aria-disabled");
      }
      var panel = directChildWithClass(item, "layui-menu-body-panel");
      var nested = panel && (directChildWithClass(panel, "layui-menu")
        || directChildWithClass(panel, "layui-dropdown-menu")
        || directChildByTag(panel, "UL"));
      if (!nested && item.classList.contains("layui-menu-item-group")) {
        nested = directChildWithClass(item, "layui-menu") || directChildByTag(item, "UL");
      }
      if (nested) {
        var titleId = title ? ensureId(title, "layui-submenu-trigger") : "";
        if (title) {
          title.setAttribute("aria-haspopup", "menu");
          title.setAttribute(
            "aria-expanded",
            item.classList.contains("cp-menu-open") ? "true" : "false",
          );
        }
        syncMenuList(nested, label, titleId);
      }
    });
  }

  function visibleMenuItems(menu) {
    return Array.prototype.filter.call(
      menu.querySelectorAll('.layui-menu-body-title[role="menuitem"]'),
      function (item) {
        if (item.getAttribute("aria-disabled") === "true") return false;
        var style = window.getComputedStyle(item);
        var rect = item.getBoundingClientRect();
        return style.display !== "none" && style.visibility !== "hidden"
          && rect.width > 0 && rect.height > 0;
      },
    );
  }

  function submenuItems(item) {
    var panel = item && item.parentElement
      && directChildWithClass(item.parentElement, "layui-menu-body-panel");
    return panel ? visibleMenuItems(panel) : [];
  }

  function syncDropdown(menu) {
    var trigger = dropdownTrigger(menu);
    var fallbackTrigger = false;
    var root = directChildWithClass(menu, "layui-menu")
      || directChildWithClass(menu, "layui-dropdown-menu");
    if (!trigger && lastInteractionTarget && lastInteractionTarget.isConnected) {
      trigger = lastInteractionTarget;
      menu.__cpA11yTrigger = trigger;
      fallbackTrigger = true;
      if (fallbackDropdownTriggers.indexOf(trigger) === -1) fallbackDropdownTriggers.push(trigger);
    }
    if (!trigger || !root) return;
    var label = accessibleLabel(trigger, "操作菜单");
    var rootId = ensureId(root, "layui-dropdown-menu");
    menu.setAttribute("role", "presentation");
    trigger.setAttribute("aria-haspopup", "menu");
    trigger.setAttribute("aria-controls", rootId);
    trigger.setAttribute("aria-expanded", "true");
    syncMenuList(root, label, "");

    if (fallbackTrigger && !menu.__cpA11yFocused) {
      menu.__cpA11yFocused = true;
      var fallbackItems = visibleMenuItems(menu);
      if (fallbackItems[0]) window.setTimeout(function () { fallbackItems[0].focus(); }, 0);
    }

    if (pendingDropdownFocus && pendingDropdownFocus.trigger === trigger) {
      var items = visibleMenuItems(menu);
      var target = pendingDropdownFocus.last ? items[items.length - 1] : items[0];
      pendingDropdownFocus = null;
      if (target) window.setTimeout(function () { target.focus(); }, 0);
    }
  }

  function syncDropdowns() {
    var menus = Array.prototype.slice.call(document.querySelectorAll(".layui-dropdown"));
    menus.forEach(syncDropdown);
    fallbackDropdownTriggers = fallbackDropdownTriggers.filter(function (trigger) {
      return trigger && trigger.isConnected;
    });
    var triggers = Array.prototype.slice.call(document.querySelectorAll("[lay-dropdown-id]"))
      .filter(function (node) { return !node.classList.contains("layui-dropdown"); });
    fallbackDropdownTriggers.forEach(function (trigger) {
      if (triggers.indexOf(trigger) === -1) triggers.push(trigger);
    });
    triggers.forEach(function (trigger) {
      if (trigger.classList.contains("layui-dropdown")) return;
      var menu = menus.find(function (candidate) {
        return dropdownTrigger(candidate) === trigger;
      }) || dropdownForTrigger(trigger);
      trigger.setAttribute("aria-haspopup", "menu");
      trigger.setAttribute("aria-expanded", menu ? "true" : "false");
      if (!menu) trigger.removeAttribute("aria-controls");
    });
  }

  function setSubmenuOpen(item, open) {
    var row = item && item.parentElement;
    if (!row || !row.classList.contains("layui-menu-item-parent")) return false;
    if (open) {
      Array.prototype.forEach.call(row.parentElement.children, function (sibling) {
        if (sibling !== row) sibling.classList.remove("cp-menu-open");
      });
    }
    row.classList.toggle("cp-menu-open", open);
    item.setAttribute("aria-expanded", open ? "true" : "false");
    return true;
  }

  function closeDropdown(menu, restoreFocus) {
    var trigger = dropdownTrigger(menu);
    var dropdownId = menu && menu.getAttribute("lay-dropdown-id");
    var linkedTrigger = trigger && dropdownId
      && (trigger.id === dropdownId || trigger.getAttribute("lay-dropdown-id") === dropdownId);
    if (linkedTrigger && menu && menu.isConnected) trigger.click();
    else if (menu && menu.isConnected) menu.remove();
    if (trigger) {
      trigger.setAttribute("aria-expanded", "false");
      trigger.removeAttribute("aria-controls");
      if (restoreFocus) window.setTimeout(function () { trigger.focus(); }, 0);
    }
  }

  function focusRelativeTo(trigger, backwards) {
    var focusable = Array.prototype.filter.call(document.querySelectorAll([
      'a[href]', 'button:not([disabled])', 'input:not([disabled]):not([type="hidden"])',
      'select:not([disabled])', 'textarea:not([disabled])', 'summary',
      '[tabindex]:not([tabindex="-1"])',
    ].join(",")), function (node) {
      if (node.closest(".layui-dropdown") || node.closest("[inert]")) return false;
      var style = window.getComputedStyle(node);
      var rect = node.getBoundingClientRect();
      return style.display !== "none" && style.visibility !== "hidden"
        && rect.width > 0 && rect.height > 0;
    });
    var index = focusable.indexOf(trigger);
    var target = backwards ? focusable[index - 1] : focusable[index + 1];
    if (target && typeof target.focus === "function") target.focus();
  }

  function handleDropdownKeys(event) {
    var target = event.target;
    if (!target || typeof target.closest !== "function") return false;
    var trigger = target.closest("[lay-dropdown-id]");
    if (trigger && trigger.classList.contains("layui-dropdown")) trigger = null;
    var menu = trigger ? dropdownForTrigger(trigger) : target.closest(".layui-dropdown");
    if (trigger) {
      if (event.key === "Escape" && menu) {
        event.preventDefault();
        closeDropdown(menu, true);
        return true;
      }
      if (["Enter", " ", "ArrowDown", "ArrowUp"].indexOf(event.key) === -1) return false;
      event.preventDefault();
      if (menu) {
        if (event.key === "Enter" || event.key === " ") closeDropdown(menu, true);
        else {
          var openItems = visibleMenuItems(menu);
          var openTarget = event.key === "ArrowUp" ? openItems[openItems.length - 1] : openItems[0];
          if (openTarget) openTarget.focus();
        }
      } else {
        pendingDropdownFocus = { trigger: trigger, last: event.key === "ArrowUp" };
        trigger.click();
        scheduleSync();
      }
      return true;
    }
    if (!menu) return false;
    var item = target.closest('.layui-menu-body-title[role="menuitem"]');
    if (!item) return false;
    var panel = item.closest(".layui-menu-body-panel");
    var parentRow = panel && panel.parentElement;
    var parentItem = parentRow && directChildWithClass(parentRow, "layui-menu-body-title");
    var items = visibleMenuItems(menu);
    var index = items.indexOf(item);

    if (event.key === "ArrowDown" || event.key === "ArrowUp"
        || event.key === "Home" || event.key === "End") {
      event.preventDefault();
      var nextIndex = event.key === "Home" ? 0
        : event.key === "End" ? items.length - 1
        : event.key === "ArrowDown" ? (index + 1) % items.length
        : (index - 1 + items.length) % items.length;
      if (items[nextIndex]) items[nextIndex].focus();
      return true;
    }
    if (event.key === "ArrowRight" && item.getAttribute("aria-haspopup") === "menu") {
      event.preventDefault();
      setSubmenuOpen(item, true);
      var nestedItems = submenuItems(item);
      if (nestedItems[0]) nestedItems[0].focus();
      return true;
    }
    if (event.key === "ArrowLeft" && parentItem) {
      event.preventDefault();
      setSubmenuOpen(parentItem, false);
      parentItem.focus();
      return true;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      if (parentItem) {
        setSubmenuOpen(parentItem, false);
        parentItem.focus();
      } else closeDropdown(menu, true);
      return true;
    }
    if (event.key === "Tab") {
      event.preventDefault();
      var dropdownButton = dropdownTrigger(menu);
      closeDropdown(menu, false);
      window.setTimeout(function () { focusRelativeTo(dropdownButton, event.shiftKey); }, 0);
      return true;
    }
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      if (item.getAttribute("aria-haspopup") === "menu") {
        setSubmenuOpen(item, item.getAttribute("aria-expanded") !== "true");
        var childItems = submenuItems(item);
        if (childItems[0]) childItems[0].focus();
      } else item.click();
      scheduleSync();
      return true;
    }
    return false;
  }

  function layerShade(layer) {
    var times = layer.getAttribute("times");
    var shades = document.querySelectorAll(".layui-layer-shade");
    for (var index = 0; index < shades.length; index += 1) {
      if (!times || shades[index].getAttribute("times") === times) return shades[index];
    }
    return null;
  }

  function visibleFocusableWithin(container) {
    var selector = [
      'a[href]',
      'button:not([disabled])',
      'input:not([disabled]):not([type="hidden"])',
      'select:not([disabled])',
      'textarea:not([disabled])',
      '[role="button"][tabindex]',
      '[tabindex]:not([tabindex="-1"])',
    ].join(",");
    return Array.prototype.filter.call(container.querySelectorAll(selector), function (node) {
      var style = window.getComputedStyle(node);
      var rect = node.getBoundingClientRect();
      return style.display !== "none" && style.visibility !== "hidden"
        && rect.width > 0 && rect.height > 0;
    });
  }

  function restoreModalBackground() {
    modalBackgroundState.forEach(function (entry) {
      if (entry.node && entry.node.isConnected) entry.node.inert = entry.inert;
    });
    modalBackgroundState = [];
  }

  function deactivateDetachedModal() {
    if (!activeModal || activeModal.isConnected) return;
    var trigger = activeModal.__cpA11yTrigger;
    restoreModalBackground();
    activeModal = null;
    if (trigger && trigger.isConnected && typeof trigger.focus === "function") {
      window.setTimeout(function () { trigger.focus(); }, 0);
    }
  }

  function activateModal(layer, shade) {
    if (activeModal === layer) return;
    if (activeModal && activeModal.isConnected) return;
    deactivateDetachedModal();
    activeModal = layer;
    layer.__cpA11yTrigger = lastInteractionTarget && lastInteractionTarget.isConnected
      ? lastInteractionTarget
      : null;
    modalBackgroundState = [];
    Array.prototype.forEach.call(document.body.children, function (node) {
      if (node === layer || node === shade || node.contains(layer)) return;
      modalBackgroundState.push({ node: node, inert: Boolean(node.inert) });
      node.inert = true;
    });
    if (shade) shade.setAttribute("aria-hidden", "true");
    window.setTimeout(function () {
      if (!layer.isConnected) return;
      var target = layer.querySelector("input:not([disabled]), textarea:not([disabled]), select:not([disabled])")
        || layer.querySelector(".layui-layer-btn1")
        || layer.querySelector(".layui-layer-btn0")
        || layer.querySelector(".layui-layer-close")
        || layer;
      if (target && typeof target.focus === "function") target.focus();
    }, 0);
  }

  function syncLayer(layer) {
    if (layer.classList.contains("layui-layer-loading")) {
      layer.setAttribute("role", "status");
      layer.setAttribute("aria-live", "polite");
      layer.setAttribute("aria-label", "正在加载");
      layer.setAttribute("aria-busy", "true");
      layer.setAttribute("aria-atomic", "true");
      return;
    }
    if (layer.classList.contains("layui-layer-tips")) {
      layer.setAttribute("role", "tooltip");
      return;
    }
    if (layer.classList.contains("layui-layer-msg")) {
      layer.setAttribute("role", "status");
      layer.setAttribute("aria-live", "polite");
      layer.setAttribute("aria-atomic", "true");
      return;
    }
    var content = layer.querySelector(".layui-layer-content");
    var title = layer.querySelector(".layui-layer-title");
    var actions = layer.querySelector(".layui-layer-btn");
    var shade = layerShade(layer);
    var alertDialog = layer.classList.contains("layui-layer-dialog") && Boolean(actions);
    layer.setAttribute("role", alertDialog ? "alertdialog" : "dialog");
    layer.setAttribute("aria-modal", shade ? "true" : "false");
    layer.setAttribute("tabindex", "-1");
    if (title && cleanText(title.textContent)) {
      layer.setAttribute("aria-labelledby", ensureId(title, "layui-dialog-title"));
      layer.removeAttribute("aria-label");
    } else {
      layer.setAttribute("aria-label", alertDialog ? "确认操作" : "对话框");
      layer.removeAttribute("aria-labelledby");
    }
    if (content && cleanText(content.textContent)) {
      layer.setAttribute("aria-describedby", ensureId(content, "layui-dialog-content"));
    } else {
      layer.removeAttribute("aria-describedby");
    }
    layer.querySelectorAll("iframe").forEach(function (frame) {
      if (!cleanText(frame.getAttribute("title"))) {
        frame.setAttribute("title", cleanText(title && title.textContent) || "对话框内容");
      }
    });
    layer.querySelectorAll(".layui-layer-btn a, .layui-layer-close").forEach(function (action) {
      action.setAttribute("role", "button");
      action.setAttribute("tabindex", "0");
      if (!cleanText(action.textContent) && !action.getAttribute("aria-label")) {
        action.setAttribute("aria-label", "关闭对话框");
      }
    });
    if (shade) activateModal(layer, shade);
  }

  function sync() {
    deactivateDetachedModal();
    document.querySelectorAll(".layui-form-select").forEach(syncSelect);
    document.querySelectorAll(
      ".layui-form-checkbox, .layui-form-radio, .layui-form-switch",
    ).forEach(syncChoice);
    document.querySelectorAll(".layui-colla-title").forEach(syncCollapse);
    document.querySelectorAll("xm-select").forEach(syncXmSelect);
    document.querySelectorAll("#tv_charts_area iframe").forEach(function (frame) {
      if (!cleanText(frame.getAttribute("title"))) {
        frame.setAttribute("title", "当前标的行情与缠论图");
      }
    });
    syncDropdowns();
    document.querySelectorAll(".layui-layer").forEach(syncLayer);
  }

  function scheduleSync() {
    if (syncScheduled) return;
    syncScheduled = true;
    var enqueue = window.requestAnimationFrame || function (callback) {
      return window.setTimeout(callback, 0);
    };
    enqueue(function () {
      syncScheduled = false;
      sync();
    });
  }

  function choiceFromEvent(event) {
    var target = event.target;
    if (!target || typeof target.closest !== "function") return null;
    return target.closest(".layui-form-checkbox, .layui-form-radio, .layui-form-switch");
  }

  function rememberInteraction(event) {
    var target = event.target;
    if (!target || typeof target.closest !== "function") return;
    var interactive = target.closest(
      'button, a[href], input, select, textarea, summary, [role="button"], [tabindex], .layui-table-body tr',
    );
    var dropdown = interactive && interactive.closest(".layui-dropdown");
    if (dropdown) interactive = dropdownTrigger(dropdown) || interactive;
    if (interactive && !interactive.closest(".layui-layer")) lastInteractionTarget = interactive;
  }

  function closeActiveModal() {
    if (!activeModal || !activeModal.isConnected) return false;
    var cancel = activeModal.querySelector(".layui-layer-btn1");
    if (cancel) {
      cancel.click();
      return true;
    }
    var close = activeModal.querySelector(".layui-layer-close");
    if (close) {
      close.click();
      return true;
    }
    var match = String(activeModal.id || "").match(/(\d+)$/);
    var layerApi = (window.layui && window.layui.layer) || window.layer;
    if (match && layerApi && typeof layerApi.close === "function") {
      layerApi.close(Number(match[1]));
      return true;
    }
    return false;
  }

  function handleModalKeys(event) {
    if (!activeModal || !activeModal.isConnected) return false;
    if (event.key === "Escape") {
      if (closeActiveModal()) {
        event.preventDefault();
        event.stopPropagation();
      }
      return true;
    }
    if (event.key !== "Tab") return false;
    var focusable = visibleFocusableWithin(activeModal);
    if (!focusable.length) {
      event.preventDefault();
      activeModal.focus();
      return true;
    }
    var first = focusable[0];
    var last = focusable[focusable.length - 1];
    if (!activeModal.contains(document.activeElement)) {
      event.preventDefault();
      first.focus();
    } else if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
    return true;
  }

  function start() {
    sync();
    document.addEventListener("keydown", function (event) {
      handleSelectKeys(event);
    }, true);
    document.addEventListener("keydown", function (event) {
      rememberInteraction(event);
      if (handleModalKeys(event)) return;
      if (handleDropdownKeys(event)) return;
      if (handleSelectKeys(event)) return;
      if (handleXmSelectKeys(event)) return;
      if (event.key !== " " && event.key !== "Enter") return;
      var choice = choiceFromEvent(event);
      if (choice && choice.getAttribute("aria-disabled") !== "true") {
        event.preventDefault();
        choice.click();
        window.setTimeout(sync, 0);
        return;
      }
      var target = event.target;
      var collapse = target && target.closest && target.closest(".layui-colla-title[role=\"button\"]");
      var layerAction = target && target.closest && target.closest(
        ".layui-layer [role=\"button\"]",
      );
      var action = collapse || layerAction;
      if (!action) return;
      event.preventDefault();
      action.click();
      window.setTimeout(sync, 0);
    });
    document.addEventListener("pointerdown", rememberInteraction, true);
    document.addEventListener("contextmenu", rememberInteraction, true);
    document.addEventListener("pointerover", function (event) {
      var target = event.target;
      var item = target && target.closest
        && target.closest('.layui-menu-item-parent > .layui-menu-body-title[role="menuitem"]');
      if (item) setSubmenuOpen(item, true);
    }, true);
    document.addEventListener("click", function (event) {
      rememberInteraction(event);
      scheduleSync();
    }, true);
    document.addEventListener("change", scheduleSync, true);
    if (typeof window.MutationObserver === "function" && document.body) {
      new window.MutationObserver(scheduleSync).observe(document.body, {
        childList: true,
        subtree: true,
        attributes: true,
        attributeFilter: ["class", "checked", "disabled"],
      });
    }
  }

  window.LayuiAccessibility = { sync: sync };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})(window, document);
