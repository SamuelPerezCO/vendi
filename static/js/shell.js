/* ---------------------------------------------------------------------------
 * Shell glue.
 *
 * HTMX swaps only part of the page, so any nav that sits *outside* the swapped
 * region has to move its own active state. That applies to two navs now:
 *   - the sidebar        (outside #content, never re-rendered)
 *   - the Inbox nav panel (inside #content, but only column 3 is swapped)
 *
 * Rather than special-case each, any container marked [data-nav-group] gets the
 * behaviour: clicking a [data-nav-item] inside it moves `is-active` to that item.
 *
 * Also handled here: document.title, which lives in <head>, outside every swap,
 * and the welcome screen's empty state -- "/" selects no icon at all, so the
 * rail has to be cleared rather than pointed at a default.
 *
 * All of this is progressive enhancement. With JS off every nav item is still a
 * plain link, and the server renders the correct active state on a full load.
 *
 * Note: listeners are delegated from `document` and elements are looked up on
 * demand rather than cached. A cached node goes stale the moment anything
 * re-renders its subtree, leaving clicks updating a detached element.
 * ------------------------------------------------------------------------- */
(function () {
  "use strict";

  var ACTIVE = "is-active";

  /** Drop the active state from every item in `group`. */
  function clearActive(group) {
    if (!group) return;
    group.querySelectorAll("[data-nav-item]." + ACTIVE).forEach(function (el) {
      el.classList.remove(ACTIVE);
      el.removeAttribute("aria-current");
    });
  }

  /** Move the active state to `item`, clearing its siblings in the same group. */
  function setActive(item) {
    if (!item) return;
    var group = item.closest("[data-nav-group]");
    if (!group) return;

    clearActive(group);

    item.classList.add(ACTIVE);
    // The sidebar navigates between pages; the Inbox nav filters one list.
    item.setAttribute("aria-current", item.closest(".sidebar") ? "page" : "true");
  }

  /** The sidebar rail, or null before it has rendered. */
  function getRail() {
    return document.querySelector(".sidebar");
  }

  // Instant feedback: don't wait for the fragment to come back.
  document.addEventListener("click", function (event) {
    var item = event.target.closest("[data-nav-item]");
    if (item) {
      setActive(item);
      return;
    }

    // Shortcuts that swap #content from outside the rail -- the welcome
    // screen's quick-access cards -- name the icon they correspond to, so the
    // rail can follow a navigation it did not originate.
    var shortcut = event.target.closest("[data-nav-for]");
    if (!shortcut) return;
    var rail = getRail();
    if (rail) {
      setActive(rail.querySelector('.nav-item[href="' + shortcut.dataset.navFor + '"]'));
    }
  });

  /** Re-derive the sidebar's active icon from the address bar (back/forward). */
  function syncSidebarFromUrl() {
    var rail = getRail();
    if (!rail) return;

    var match = rail.querySelector(
      '.nav-item[href="' + window.location.pathname + '"]'
    );

    // "/" is the welcome screen and matches no icon. Clear the rail rather than
    // leaving the previous section's pill lit -- setActive(null) is a no-op, so
    // the empty case has to be handled explicitly.
    if (match) setActive(match);
    else clearActive(rail);
  }

  window.addEventListener("popstate", syncSidebarFromUrl);
  document.addEventListener("htmx:historyRestore", syncSidebarFromUrl);

  // Keep the tab title in step with whatever section is showing.
  function syncTitle() {
    var heading = document.querySelector("#content .page-head__title");
    if (heading) document.title = heading.textContent.trim() + " · Vendi";
  }

  document.addEventListener("htmx:afterSwap", syncTitle);
  document.addEventListener("htmx:historyRestore", syncTitle);

  /* -------------------------------------------------------------------------
   * Dismissible banners.
   *
   * Any element with [data-dismissible="<key>"] can contain a [data-dismiss]
   * button. Clicking it hides the element and records the key in localStorage,
   * so the banner stays gone across reloads -- and across HTMX swaps, which
   * re-render it fresh from the server: after every swap the stored keys are
   * re-applied before the user sees the frame settle.
   *
   * localStorage access is wrapped because it can throw (private windows,
   * blocked site data); in that case the banner simply returns next page load.
   * ---------------------------------------------------------------------- */

  var DISMISS_PREFIX = "dismissed:";

  function isDismissed(key) {
    try { return window.localStorage.getItem(DISMISS_PREFIX + key) === "1"; }
    catch (e) { return false; }
  }

  function rememberDismissed(key) {
    try { window.localStorage.setItem(DISMISS_PREFIX + key, "1"); }
    catch (e) { /* no persistence available -- hide for this view only */ }
  }

  /** Hide every banner in `root` the user has already dismissed. */
  function hideDismissed(root) {
    (root || document).querySelectorAll("[data-dismissible]").forEach(function (el) {
      if (isDismissed(el.dataset.dismissible)) el.hidden = true;
    });
  }

  document.addEventListener("click", function (event) {
    var btn = event.target.closest("[data-dismiss]");
    if (!btn) return;
    var banner = btn.closest("[data-dismissible]");
    if (!banner) return;
    banner.hidden = true;
    rememberDismissed(banner.dataset.dismissible);
  });

  document.addEventListener("htmx:afterSwap", function (event) {
    hideDismissed(event.target);
  });
  document.addEventListener("htmx:historyRestore", function () {
    hideDismissed(document);
  });

  // First paint: the script is deferred, so the DOM is already parsed.
  hideDismissed(document);

  /* -------------------------------------------------------------------------
   * Chat scroll position.
   *
   * The Inbox thread (#chat-messages) re-renders every few seconds (poll) and
   * on every send. A chat should rest at the newest message, so scroll to the
   * bottom after those swaps -- but only when the user was already there:
   * someone scrolled up reading history must not be yanked back down by a
   * poll. "There" is measured just before the swap, with a small tolerance.
   *
   * Pictures move everything. A photo or sticker has no height until it
   * loads, and a swap re-inserts every one of them, so right after a swap the
   * list is shorter than it is about to be -- in a sticker-heavy chat shorter
   * than the box itself, at which point the browser clamps scrollTop to 0.
   * The pictures then load, the list grows back by thousands of pixels, and
   * the view stays wherever the clamp left it: at the first message of the
   * conversation, on every poll. Chrome's scroll anchoring hides most of
   * this; Safari has no scroll anchoring at all. inbox.css reserves each
   * picture's box up front so the list rarely changes height, and this keeps
   * the reader's place through whatever change remains:
   *
   * - pinned: the reader rests at the newest message, and every swap,
   *   picture load and resize scrolls back to the bottom. Only the reader's
   *   own input unpins -- the wheel, a touch drag, the scrollbar, a scroll
   *   key -- never a scrollTop the browser moved by itself.
   * - anchored: the reader is somewhere else, and the message at the top of
   *   the view keeps its offset through every swap and resize.
   * ---------------------------------------------------------------------- */

  var chatPinned = false;
  var chatAnchor = null;  // {id, offset} of the message at the top of the view
  var chatInputAt = 0;    // when the reader last scrolled the thread themselves

  /* How far from the bottom still counts as "at the bottom". This only has to
     absorb sub-pixel rounding -- scrollTop comes back fractional (3821.5 for a
     maximum of 3821) on a scaled display, and an exact comparison would then
     never be true. It must NOT be big enough to swallow a real gesture: at 48
     it did, and a reader who nudged the thread up a little to re-read
     something got dragged back to the newest message a few seconds later by
     the poll, over and over, with nothing on screen explaining why. Content
     growing between polls is already handled -- "was at the bottom" is
     measured before the swap, not after -- so the slack buys nothing else. */
  var CHAT_BOTTOM_SLACK = 4;

  function isChatBox(el) {
    return el && el.id === "chat-messages";
  }

  function inChat(node) {
    return node && node.closest ? node.closest("#chat-messages") : null;
  }

  function nearBottom(box) {
    return box.scrollHeight - box.scrollTop - box.clientHeight < CHAT_BOTTOM_SLACK;
  }

  // The first message still (partly) below the box's top edge, and where its
  // top sits relative to that edge. Messages carry data-message-id so the
  // same one can be found again in the swapped-in list. They are in document
  // order, so a binary search finds it in a handful of rect reads however
  // long the thread is.
  function messageAtTop(box) {
    var top = box.getBoundingClientRect().top;
    var messages = box.querySelectorAll(".msg[data-message-id]");
    var low = 0;
    var high = messages.length - 1;
    var found = null;
    while (low <= high) {
      var mid = (low + high) >> 1;
      var rect = messages[mid].getBoundingClientRect();
      if (rect.bottom > top) {
        found = { id: messages[mid].dataset.messageId, offset: rect.top - top };
        high = mid - 1;
      } else {
        low = mid + 1;
      }
    }
    return found;
  }

  function rememberPlace(box) {
    chatAnchor = chatPinned ? null : messageAtTop(box);
  }

  // Once per frame at most: momentum scrolling fires dozens of scroll events
  // a second, and one anchor per painted frame is all that is ever needed.
  var placePending = false;
  function rememberPlaceSoon(box) {
    if (placePending) return;
    placePending = true;
    requestAnimationFrame(function () {
      placePending = false;
      if (document.contains(box)) rememberPlace(box);
    });
  }

  // Put the view back where the reader had it: the bottom, or the anchored
  // message at its offset. Idempotent, so it runs after every swap, picture
  // load and resize, and does nothing wherever the browser already got it
  // right (Chrome's own anchoring, say).
  function keepPlace(box) {
    if (chatPinned) {
      box.scrollTop = box.scrollHeight;
      return;
    }
    if (!chatAnchor) return;
    var message = box.querySelector('.msg[data-message-id="' + chatAnchor.id + '"]');
    if (!message) return;
    var drift = message.getBoundingClientRect().top - box.getBoundingClientRect().top
      - chatAnchor.offset;
    if (Math.abs(drift) >= 1) box.scrollTop += drift;
  }

  // A photo's placeholder (inbox.css) gives way to the photo once it has
  // loaded, or failed. Cached pictures can be complete the moment they are
  // inserted, before any load event reaches us, hence the sweep.
  function markLoadedPictures(box) {
    box.querySelectorAll(".msg__image img").forEach(function (img) {
      if (img.complete) img.parentElement.classList.add("is-loaded");
    });
  }

  // Height changes inside the thread -- a picture loading, a placeholder
  // giving way -- are corrected before they paint. Re-armed after every swap,
  // since a swap replaces every child. The box itself is watched too: the
  // composer under it changes height when the 24h window flips.
  var chatResizes = window.ResizeObserver
    ? new ResizeObserver(function (entries) {
        var box = inChat(entries[0].target);
        if (box) keepPlace(box);
      })
    : null;

  function watchChat(box) {
    if (!chatResizes) return;
    chatResizes.disconnect();
    chatResizes.observe(box);
    box.querySelectorAll(".msg").forEach(function (message) {
      chatResizes.observe(message);
    });
  }

  function settleChat(box) {
    markLoadedPictures(box);
    keepPlace(box);
    watchChat(box);
  }

  document.addEventListener("htmx:beforeSwap", function (event) {
    var box = event.detail.target;
    if (!isChatBox(box)) return;
    // The composer posts to this same target, so a send lands here too.
    // Sending is an explicit "show me the newest" -- jump to it wherever the
    // reader was, or their own message arrives off-screen above the fold.
    // Only the 5s poll (a GET) has to leave a scrolled-up reader alone.
    var config = event.detail.requestConfig;
    var sent = config && String(config.verb).toLowerCase() === "post";
    if (sent || nearBottom(box)) chatPinned = true;
    rememberPlace(box);
  });

  document.addEventListener("htmx:afterSwap", function (event) {
    var target = event.detail.target;
    var box = isChatBox(target)
      ? target
      : target.querySelector && target.querySelector("#chat-messages"); // thread just opened
    if (!box) return;
    // A freshly-opened thread always starts at the newest message.
    if (!isChatBox(target)) chatPinned = true;
    settleChat(box);
  });

  // The reader scrolling the thread themselves: the wheel, a touch drag, the
  // scrollbar (a press on the box itself rather than on a message), or a
  // scroll key while no text field has focus. Upwards unpins; downwards, or
  // a click on a message or a picture, leaves the pin alone.
  function readerScrolled(upwards) {
    chatInputAt = Date.now();
    if (upwards) chatPinned = false;
  }
  document.addEventListener("wheel", function (event) {
    if (inChat(event.target)) readerScrolled(event.deltaY < 0);
  }, { capture: true, passive: true });
  document.addEventListener("touchmove", function (event) {
    if (inChat(event.target)) readerScrolled(true);
  }, { capture: true, passive: true });
  document.addEventListener("pointerdown", function (event) {
    if (isChatBox(event.target)) readerScrolled(true);
  }, true);
  document.addEventListener("keydown", function (event) {
    var target = event.target;
    var typing = /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName) || target.isContentEditable;
    if (typing) return;
    if (["PageUp", "ArrowUp", "Home"].indexOf(event.key) !== -1) readerScrolled(true);
    if (["PageDown", "ArrowDown", "End", " "].indexOf(event.key) !== -1) readerScrolled(false);
  }, true);

  // scroll does not bubble either. A scroll the reader just made that lands
  // at the bottom pins the thread again; one the browser made on its own --
  // the clamp after a swap, a picture growing -- must not, or a reader
  // partway up would be dragged to the bottom by the next picture to load.
  document.addEventListener("scroll", function (event) {
    var box = event.target;
    if (!isChatBox(box)) return;
    if (Date.now() - chatInputAt < 500 && nearBottom(box)) chatPinned = true;
    rememberPlaceSoon(box);
  }, true);

  // load and error do not bubble, hence the capture phase. A picture that
  // fails still changes its bubble's height: the alt text takes its place.
  function pictureSettled(event) {
    var img = event.target;
    if (!img || img.tagName !== "IMG") return;
    var box = inChat(img); // null: swapped out before it loaded
    if (!box) return;
    if (img.parentElement.classList.contains("msg__image")) {
      img.parentElement.classList.add("is-loaded");
    }
    keepPlace(box);
  }
  document.addEventListener("load", pictureSettled, true);
  document.addEventListener("error", pictureSettled, true);

  // Full-page load with ?chat= in the URL renders the thread server-side.
  var initialChat = document.getElementById("chat-messages");
  if (initialChat) {
    chatPinned = true;
    settleChat(initialChat);
  }

  /* -------------------------------------------------------------------------
   * Dialogs (the Etiquetas create/edit modals, the template chooser).
   *
   * Any [data-dialog-open="<id>"] control opens the <dialog> with that id;
   * [data-dialog-close] closes the enclosing one. showModal() supplies the
   * focus trap, Escape handling and focus return natively. A dialog marked
   * [data-dialog-backdrop-close] also closes on a backdrop click, and any
   * element marked [data-close-on-success] closes its enclosing dialog once
   * its HTMX request succeeds (forms additionally reset) -- the response has
   * already re-rendered the region behind it.
   * ---------------------------------------------------------------------- */

  // True while the last pointer press began on a backdrop-close dialog's
  // ::backdrop. A click alone can't tell: a drag that starts on the dialog's
  // content and ends past its edge also reports the dialog as target with
  // outside coordinates, and only the press location separates the two.
  var pressOnBackdrop = false;

  function outsideDialogBox(dialog, event) {
    var box = dialog.getBoundingClientRect();
    return event.clientX < box.left || event.clientX > box.right ||
           event.clientY < box.top || event.clientY > box.bottom;
  }

  document.addEventListener("pointerdown", function (event) {
    pressOnBackdrop =
      event.target.matches &&
      event.target.matches("dialog[data-dialog-backdrop-close]") &&
      event.target.open &&
      outsideDialogBox(event.target, event);
  });

  document.addEventListener("click", function (event) {
    var opener = event.target.closest("[data-dialog-open]");
    if (opener) {
      var dialog = document.getElementById(opener.dataset.dialogOpen);
      // A screen's own script may have opened it already (showModal on an
      // open dialog throws).
      if (dialog && !dialog.open) dialog.showModal();
      return;
    }
    var closer = event.target.closest("[data-dialog-close]");
    if (closer) {
      var enclosing = closer.closest("dialog");
      if (enclosing) enclosing.close();
      return;
    }
    // A ::backdrop click reports the <dialog> itself as target -- but so do
    // clicks on its padding and drags that end past its edge, so only close
    // when the press started on the backdrop and the release stayed outside.
    // detail === 1 skips the tail of a double-click on the opener (its second
    // press lands on the backdrop of the freshly-opened dialog) as well as
    // keyboard-synthesized clicks, which report detail 0 at (0,0).
    var backdrop = event.target;
    if (backdrop.matches && backdrop.matches("dialog[data-dialog-backdrop-close]") &&
        backdrop.open && event.detail === 1 && pressOnBackdrop &&
        outsideDialogBox(backdrop, event)) {
      backdrop.close();
    }
  });

  document.addEventListener("htmx:afterRequest", function (event) {
    var el = event.target.closest && event.target.closest("[data-close-on-success]");
    if (!el || !event.detail.successful) return;
    var dialog = el.closest("dialog");
    if (dialog && dialog.open) dialog.close();
    if (el.tagName === "FORM") {
      el.reset();
      syncTagPreview(el);
    }
  });

  // A dialog marked [data-dialog-autoshow] opens itself once its body has
  // been filled by a load-triggered fetch -- how ?nuevo=<client> on the
  // Inbox lands straight in the Nuevo chat modal. One-shot: the attribute
  // comes off so later swaps into the same body don't reopen it.
  document.addEventListener("htmx:afterSwap", function (event) {
    var dialog = event.detail.target.closest && event.detail.target.closest("dialog[data-dialog-autoshow]");
    if (!dialog) return;
    dialog.removeAttribute("data-dialog-autoshow");
    if (!dialog.open) dialog.showModal();
  });

  // The other way a dialog closes: the *response* says so. A fragment
  // carrying [data-dialog-dismiss] means the server is done with the modal
  // (the Clientes CRUD answers a successful save with one, alongside the
  // out-of-band swap that refreshes the table behind it). Response-driven,
  // because only the server knows whether a submit was accepted -- a
  // rejected one re-renders the form and the dialog has to stay open.
  document.addEventListener("htmx:afterSwap", function (event) {
    var target = event.detail.target;
    if (!target.querySelector || !target.querySelector("[data-dialog-dismiss]")) return;
    var dialog = target.closest("dialog");
    if (dialog && dialog.open) dialog.close();
  });

  // A dialog's card/link can swap away the very panel hosting the dialog --
  // and itself. The dialog is then simply removed (close() never runs on
  // that path), so the browser parks focus on <body> and announces nothing.
  // Hand focus to the fresh content's heading instead.
  document.addEventListener("htmx:afterSwap", function (event) {
    var config = event.detail.requestConfig;
    var origin = config && config.triggeringEvent && config.triggeringEvent.target;
    if (!origin || !origin.closest) return;
    if (
      !origin.closest("[data-close-on-success]") &&
      !origin.closest("[data-plantilla-form]")
    ) return;
    if (document.activeElement && document.activeElement !== document.body) return;
    // A failed editor submit lands on its first invalid field; any other
    // swap that dropped focus lands on the new content's heading.
    var target =
      event.detail.target.querySelector(".ffield--error .ffield__input") ||
      event.detail.target.querySelector("h1, h2");
    if (!target) return;
    if (!target.matches("input, select, textarea")) {
      target.setAttribute("tabindex", "-1");
    }
    target.focus();
  });

  /* -------------------------------------------------------------------------
   * Crear plantilla editor ([data-plantilla-form]): live name validation,
   * category-driven sub-type groups, header/button panels, character
   * counters, {{n}} variable insertion with sample inputs, and the WhatsApp
   * preview. Everything is delegated so it survives HTMX swaps; these rules
   * mirror core.plantillas, which re-validates every POST server-side.
   * ---------------------------------------------------------------------- */

  var TEMPLATE_NAME_RE = /^[a-z0-9_]+$/;
  // Canonical variables only ({{1}}, {{2}}... no leading zeros) -- mirrors
  // core.plantillas.VARIABLE_RE so client and server agree on what counts.
  var TEMPLATE_VARIABLE_RE = /\{\{([1-9]\d*)\}\}/g;
  var MEDIA_HEADER_LABELS = {
    image: "Imagen",
    video: "Video",
    document: "Documento",
  };

  function checkedValue(form, name) {
    var radio = form.querySelector(
      'input[name="' + name + '"]:checked:not(:disabled)'
    );
    return radio ? radio.value : "";
  }

  function bodyVariableNumbers(body) {
    var numbers = [];
    var match;
    TEMPLATE_VARIABLE_RE.lastIndex = 0;
    while ((match = TEMPLATE_VARIABLE_RE.exec(body)) !== null) {
      var number = parseInt(match[1], 10);
      if (numbers.indexOf(number) === -1) numbers.push(number);
    }
    return numbers;
  }

  // Meta's hard constraint: lowercase what was typed, flag what remains
  // invalid immediately (not only on submit).
  function syncTemplateName(input) {
    var pos = input.selectionStart;
    var lower = input.value.toLowerCase();
    if (lower !== input.value) {
      input.value = lower;
      if (pos !== null) input.setSelectionRange(pos, pos);
    }
    var invalid = input.value !== "" && !TEMPLATE_NAME_RE.test(input.value);
    var field = input.closest(".ffield");
    if (field) field.classList.toggle("ffield--error", invalid);
    input.setAttribute("aria-invalid", invalid ? "true" : "false");
  }

  // The sub-type options depend on the category: show the matching group,
  // disable the rest (disabled radios don't submit) and make sure the
  // visible group has a selection.
  function syncTemplateCategory(form) {
    var category = checkedValue(form, "category");
    form.querySelectorAll("[data-subtype-group]").forEach(function (group) {
      var active = group.dataset.subtypeGroup === category;
      group.hidden = !active;
      var radios = group.querySelectorAll('input[name="sub_type"]');
      radios.forEach(function (radio) { radio.disabled = !active; });
      if (active && !group.querySelector('input[name="sub_type"]:checked')) {
        if (radios[0]) radios[0].checked = true;
      }
    });
  }

  function syncTemplateHeader(form) {
    var kind = checkedValue(form, "header_type");
    var textPanel = form.querySelector('[data-header-panel="text"]');
    var mediaPanel = form.querySelector('[data-header-panel="media"]');
    if (textPanel) textPanel.hidden = kind !== "text";
    if (mediaPanel) mediaPanel.hidden = !MEDIA_HEADER_LABELS[kind];
    var label = form.querySelector("[data-header-media-label]");
    if (label && MEDIA_HEADER_LABELS[kind]) {
      label.textContent =
        "Sube " +
        (kind === "image" ? "una imagen" : kind === "video" ? "un video" : "un documento") +
        " de ejemplo";
    }
    // Match the file picker to the chosen kind (server re-checks the type).
    var upload = form.elements.header_media;
    if (upload) {
      upload.accept =
        kind === "image" ? "image/*" :
        kind === "video" ? "video/*" : "application/pdf";
    }
  }

  function syncTemplateButtons(form) {
    var kind = checkedValue(form, "button_kind");
    var quickPanel = form.querySelector('[data-buttons-panel="quick"]');
    var ctaPanel = form.querySelector('[data-buttons-panel="cta"]');
    if (quickPanel) quickPanel.hidden = kind !== "quick";
    if (ctaPanel) ctaPanel.hidden = kind !== "cta";
  }

  function syncTemplateCounters(form) {
    form.querySelectorAll("[data-counted-input]").forEach(function (input) {
      var field = input.closest(".ffield");
      var counter = field && field.querySelector("[data-count]");
      if (counter) {
        counter.textContent = input.value.length + "/" + input.maxLength;
      }
    });
  }

  // One sample input per {{n}} -- rebuilt as variables come and go, keeping
  // already-typed values by input name.
  function syncTemplateSamples(form) {
    var body = form.querySelector("[data-body-input]");
    var wrap = form.querySelector("[data-sample-list]");
    var host = form.querySelector("[data-sample-inputs]");
    if (!body || !wrap || !host) return;

    var kept = {};
    host.querySelectorAll("input").forEach(function (input) {
      kept[input.name] = input.value;
    });

    var numbers = bodyVariableNumbers(body.value);
    host.textContent = "";
    numbers.forEach(function (number) {
      var field = document.createElement("div");
      field.className = "ffield";
      var input = document.createElement("input");
      input.className = "ffield__input";
      input.type = "text";
      input.name = "sample_" + number;
      input.id = "tpl-sample-" + number;
      input.placeholder = " ";
      input.value = kept[input.name] || "";
      var label = document.createElement("label");
      label.className = "ffield__label";
      label.htmlFor = input.id;
      label.textContent = "Ejemplo para {{" + number + "}}";
      field.appendChild(input);
      field.appendChild(label);
      host.appendChild(field);
    });
    wrap.hidden = numbers.length === 0;
  }

  // Re-render the WhatsApp bubble from the form: {{n}} replaced by its
  // sample value (the literal {{n}} until one is typed), buttons the way
  // WhatsApp draws them. Everything set via textContent -- never markup.
  function syncTemplatePreview(form) {
    var preview = document.querySelector("[data-wa-preview]");
    if (!preview) return;

    var headerKind = checkedValue(form, "header_type");
    var headerText = headerKind === "text" ? form.elements.header_text.value : "";
    var mediaLabel = MEDIA_HEADER_LABELS[headerKind] || "";

    var samples = {};
    form.querySelectorAll("[data-sample-inputs] input").forEach(function (input) {
      samples[input.name] = input.value;
    });
    var body = form.elements.body.value.replace(
      TEMPLATE_VARIABLE_RE,
      function (token, number) {
        return samples["sample_" + number] || token;
      }
    );
    var footer = form.elements.footer.value;

    var buttonTexts = [];
    var buttonKind = checkedValue(form, "button_kind");
    if (buttonKind === "quick") {
      [1, 2, 3].forEach(function (i) {
        var text = form.elements["quick_reply_" + i].value.trim();
        if (text) buttonTexts.push(text);
      });
    } else if (buttonKind === "cta") {
      ["cta_url_text", "cta_phone_text"].forEach(function (name) {
        var text = form.elements[name].value.trim();
        if (text) buttonTexts.push(text);
      });
    }

    var hasContent = !!(headerText || mediaLabel || body || footer || buttonTexts.length);
    preview.querySelector("[data-wa-bubble]").hidden = !hasContent;
    preview.querySelector("[data-wa-empty]").hidden = hasContent;

    var headerEl = preview.querySelector("[data-wa-header]");
    headerEl.hidden = !headerText;
    headerEl.textContent = headerText;

    var mediaEl = preview.querySelector("[data-wa-media]");
    mediaEl.hidden = !mediaLabel;
    preview.querySelector("[data-wa-media-label]").textContent = mediaLabel;

    preview.querySelector("[data-wa-body]").textContent = body;

    var footerEl = preview.querySelector("[data-wa-footer]");
    footerEl.hidden = !footer;
    footerEl.textContent = footer;

    var buttonsEl = preview.querySelector("[data-wa-buttons]");
    buttonsEl.hidden = !buttonTexts.length;
    buttonsEl.textContent = "";
    buttonTexts.forEach(function (text) {
      var button = document.createElement("span");
      button.className = "wa-bubble__button";
      button.textContent = text;
      buttonsEl.appendChild(button);
    });
  }

  // Full sync -- on first load and after any swap that (re)renders the
  // editor. A no-op on every other screen.
  function syncTemplateEditor() {
    var form = document.querySelector("[data-plantilla-form]");
    if (!form) return;
    syncTemplateCategory(form);
    syncTemplateHeader(form);
    syncTemplateButtons(form);
    syncTemplateSamples(form);
    syncTemplateCounters(form);
    syncTemplatePreview(form);
  }

  document.addEventListener("input", function (event) {
    var form = event.target.closest && event.target.closest("[data-plantilla-form]");
    if (!form) return;
    if (event.target.matches("[data-name-input]")) syncTemplateName(event.target);
    if (event.target.matches("[data-body-input]")) syncTemplateSamples(form);
    syncTemplateCounters(form);
    syncTemplatePreview(form);
  });

  document.addEventListener("change", function (event) {
    var form = event.target.closest && event.target.closest("[data-plantilla-form]");
    if (!form) return;
    if (event.target.name === "category") syncTemplateCategory(form);
    if (event.target.name === "header_type") syncTemplateHeader(form);
    if (event.target.name === "button_kind") syncTemplateButtons(form);
    syncTemplatePreview(form);
  });

  // "+ Añadir variable": insert the next {{n}} at the cursor.
  document.addEventListener("click", function (event) {
    var trigger = event.target.closest && event.target.closest("[data-add-variable]");
    if (!trigger) return;
    var form = trigger.closest("[data-plantilla-form]");
    var body = form && form.querySelector("[data-body-input]");
    if (!body) return;
    var numbers = bodyVariableNumbers(body.value);
    var next = numbers.length ? Math.max.apply(null, numbers) + 1 : 1;
    var token = "{{" + next + "}}";
    var start = body.selectionStart === null ? body.value.length : body.selectionStart;
    var end = body.selectionEnd === null ? start : body.selectionEnd;
    body.value = body.value.slice(0, start) + token + body.value.slice(end);
    body.focus();
    body.setSelectionRange(start + token.length, start + token.length);
    syncTemplateSamples(form);
    syncTemplateCounters(form);
    syncTemplatePreview(form);
  });

  document.addEventListener("htmx:afterSwap", function () {
    syncTemplateEditor();
  });

  // Back/forward restores a snapshot without firing afterSwap.
  document.addEventListener("htmx:historyRestore", function () {
    syncTemplateEditor();
  });

  // shell.js loads deferred, so the DOM is ready by now.
  syncTemplateEditor();

  /* -------------------------------------------------------------------------
   * Tag modal live preview: the pill mirrors the name as it is typed and the
   * swatch as it is picked.
   * ---------------------------------------------------------------------- */

  function syncTagPreview(scope) {
    var input = scope.querySelector("[data-tag-name-input]");
    var preview = scope.querySelector("[data-tag-preview]");
    if (!input || !preview) return;
    preview.textContent = input.value.trim() || "ETIQUETA";
    var color = scope.querySelector('input[name="color"]:checked');
    if (color) preview.className = "tag-pill tag-pill--" + color.value;
  }

  document.addEventListener("input", function (event) {
    if (!event.target.closest || !event.target.closest("[data-tag-name-input]")) return;
    var form = event.target.closest("form");
    if (form) syncTagPreview(form);
  });

  document.addEventListener("change", function (event) {
    if (event.target.name === "color" && event.target.closest("dialog")) {
      syncTagPreview(event.target.closest("form"));
    }
  });

  /* -------------------------------------------------------------------------
   * Dropdowns ([data-dropdown] <details>): close on outside click and on
   * Escape -- <details> only closes itself on a second summary click.
   * ---------------------------------------------------------------------- */

  function closeDropdowns(except) {
    document.querySelectorAll("details[data-dropdown][open]").forEach(function (d) {
      if (d !== except) d.removeAttribute("open");
    });
  }

  document.addEventListener("click", function (event) {
    var inside = event.target.closest("details[data-dropdown]");
    closeDropdowns(inside);
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") closeDropdowns(null);
  });

  /* -------------------------------------------------------------------------
   * Conversation multi-select: the select-all box drives the row boxes, and
   * any change updates the Acciones dropdown's visibility (.has-selection on
   * the bulk form) and its count chip. List re-renders arrive unchecked, so
   * the state is re-derived after every #conv-list swap.
   * ---------------------------------------------------------------------- */

  function refreshSelection() {
    var checked = document.querySelectorAll(".conv-item__check:checked").length;
    var form = document.getElementById("bulk-form");
    if (form) form.classList.toggle("has-selection", checked > 0);
    var count = document.querySelector("[data-selection-count]");
    if (count) {
      count.hidden = checked === 0;
      count.textContent = checked;
    }
    var all = document.querySelector("[data-select-all]");
    if (all && checked === 0) all.checked = false;
  }

  document.addEventListener("change", function (event) {
    if (event.target.matches && event.target.matches("[data-select-all]")) {
      document.querySelectorAll(".conv-item__check").forEach(function (box) {
        box.checked = event.target.checked;
      });
      refreshSelection();
      return;
    }
    if (event.target.matches && event.target.matches(".conv-item__check")) {
      refreshSelection();
    }
  });

  document.addEventListener("htmx:afterSwap", function (event) {
    var target = event.detail.target;
    if (target.id === "conv-list" || target.querySelector("#conv-list")) {
      refreshSelection();
    }
  });

  /* -------------------------------------------------------------------------
   * Filterable <select> ([data-select-filter]): typing in the
   * [data-select-filter-input] narrows the options of the
   * [data-select-filter-target] select in the same container. The option
   * list is REBUILT (not hidden) because Safari's native picker ignores
   * hidden/display on <option>; the current selection always stays listed.
   * Used by the Nuevo chat modal's client list; the calendar has its own
   * copy scoped to its panel. Delegated on input, so it works on fetched
   * modal bodies.
   * ---------------------------------------------------------------------- */

  document.addEventListener("input", function (event) {
    var input = event.target.closest && event.target.closest("[data-select-filter-input]");
    if (!input) return;
    var box = input.closest("[data-select-filter]");
    var select = box && box.querySelector("[data-select-filter-target]");
    if (!select) return;
    if (!select.__allOptions) {
      select.__allOptions = Array.prototype.slice.call(select.options).map(function (o) {
        return { value: o.value, label: o.textContent.trim(), disabled: o.disabled };
      });
    }
    var query = input.value.trim().toLowerCase();
    var selected = select.value;
    var shown = 0;
    while (select.options.length) select.remove(0);
    select.__allOptions.forEach(function (entry) {
      var matches = query === "" || entry.label.toLowerCase().indexOf(query) !== -1;
      if (!matches && entry.value !== selected) return;
      var option = document.createElement("option");
      option.value = entry.value;
      option.textContent = entry.label;
      option.disabled = entry.disabled;
      select.appendChild(option);
      if (matches) shown += 1;
    });
    select.value = selected;
    // With one match, pick it: the agent typed enough to mean that one.
    if (query !== "" && shown === 1) select.selectedIndex = 0;
    var status = box.querySelector("[data-select-filter-status]");
    if (status) {
      status.textContent = query === "" ? "" :
        shown === 0 ? "Sin resultados" : shown + (shown === 1 ? " cliente" : " clientes");
    }
  });

  /* -------------------------------------------------------------------------
   * Respuestas rápidas: the composer's quick-reply picker.
   *
   * A [data-quick-send] entry posts itself to inbox_send (its own hx-post,
   * see quick_replies.html), so the reply appears in the thread on the
   * click -- nothing here does the sending, this only gets the popover out
   * of the way. All listeners are document-level delegation, so the picker
   * keeps working across every chat_thread re-render.
   * ---------------------------------------------------------------------- */

  function closeQuickReplies(except) {
    document.querySelectorAll("[data-quickreplies][open]").forEach(function (el) {
      if (el !== except) el.open = false;
    });
  }

  document.addEventListener("click", function (event) {
    // HTMX has already fired the send off this same click; just close up.
    if (event.target.closest("[data-quick-send]")) {
      closeQuickReplies();
      return;
    }
    // A click anywhere outside an open picker closes it (its own summary
    // already toggles itself -- don't fight the native behavior).
    closeQuickReplies(event.target.closest("[data-quickreplies]"));
  });

  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") return;
    var open = document.querySelector("[data-quickreplies][open]");
    if (!open) return;
    open.open = false;
    var summary = open.querySelector("summary");
    if (summary) summary.focus();
  });

  /* -------------------------------------------------------------------------
   * Enviar plantilla ([data-template-send-form]): the Inbox dialog that sends
   * a plantilla as a real WhatsApp template message.
   *
   * The dialog arrives by HTMX (fetched fresh on every click, into
   * #tpl-send-slot), so opening it is a swap concern: any swapped-in
   * <dialog data-open-on-swap> is shown modally. Inside, the <select> picks
   * which plantilla's fieldset is live -- hidden AND disabled together, so
   * the other fieldsets' required inputs neither block nor submit -- and the
   * preview re-renders {{n}} from the inputs as they are typed.
   * ---------------------------------------------------------------------- */

  document.addEventListener("htmx:afterSwap", function (event) {
    var target = event.detail.target;
    if (!target || !target.querySelector) return;
    var dialog = target.querySelector("dialog[data-open-on-swap]");
    if (dialog && !dialog.open) {
      dialog.showModal();
      syncTemplateSend(dialog);
    }
  });

  function syncTemplateSend(scope) {
    var form = scope.querySelector ? scope.querySelector("[data-template-send-form]") : null;
    if (!form && scope.matches && scope.matches("[data-template-send-form]")) form = scope;
    if (!form) return;
    var pick = form.querySelector("[data-template-pick]");
    if (!pick) return;
    form.querySelectorAll("[data-template-fields]").forEach(function (fieldset) {
      var live = fieldset.dataset.templateFields === pick.value;
      fieldset.hidden = !live;
      fieldset.disabled = !live;
      if (live) syncTemplatePreviewText(fieldset);
    });
  }

  // Mirrors core.plantillas.render_with: every {{n}} takes the value typed
  // for n, or stays as {{n}} so a blank is visible before Enviar.
  function syncTemplatePreviewText(fieldset) {
    var preview = fieldset.querySelector("[data-template-preview]");
    if (!preview) return;
    var values = {};
    fieldset.querySelectorAll("[data-template-var]").forEach(function (input) {
      values[input.dataset.templateVar] = input.value;
    });
    preview.textContent = (fieldset.dataset.templateBody || "").replace(
      /\{\{([1-9]\d*)\}\}/g,
      function (whole, number) { return values[number] || whole; }
    );
  }

  document.addEventListener("change", function (event) {
    if (event.target.matches && event.target.matches("[data-template-pick]")) {
      syncTemplateSend(event.target.closest("[data-template-send-form]"));
    }
  });

  document.addEventListener("input", function (event) {
    var input = event.target.closest && event.target.closest("[data-template-var]");
    if (input) syncTemplatePreviewText(input.closest("[data-template-fields]"));
  });

  // A rejected submit re-renders #tpl-send-body (422 + HX-Retarget, see the
  // beforeSwap rule below); the fresh markup needs its live fieldset picked.
  document.addEventListener("htmx:afterSwap", function (event) {
    if (event.detail.target && event.detail.target.id === "tpl-send-body") {
      syncTemplateSend(event.detail.target.closest("[data-template-send-form]"));
    }
  });

  /* -------------------------------------------------------------------------
   * Respuestas rápidas admin ([data-quickreply-form]): the create/edit dialog
   * on Configuración de mensajería.
   *
   * Three jobs, all delegated so they survive the panel being re-rendered:
   * a rejected submit has to land back *inside* the open dialog, the atajo
   * has to be typed in the shape the server stores, and a chosen photo has
   * to be visible before it is uploaded.
   * ---------------------------------------------------------------------- */

  // A rejected save answers 422 (see core.views._respuestas_errors_response)
  // with the dialog's fields. htmx drops 4xx bodies by default, so opt these
  // in -- but leave isError alone: `successful` stays false, which is what
  // keeps [data-close-on-success] from treating a rejection as a save.
  document.addEventListener("htmx:beforeSwap", function (event) {
    if (event.detail.xhr.status === 422) event.detail.shouldSwap = true;
  });

  /* -------------------------------------------------------------------------
   * Respuestas rápidas: live WhatsApp preview inside the drawer.
   *
   * Paints the outgoing message as the customer will receive it -- photo on
   * top, the text as its caption -- which is the shape send_image() delivers.
   * Note it previews the *body*, never the title: the title is the internal
   * label for the chat picker and the customer never sees it, which is the
   * one thing about this form agents got wrong most often.
   *
   * Delegated from the document because the form arrives by htmx swap and is
   * replaced on every failed save, so nothing here may hold a node reference
   * across renders -- each repaint re-reads the DOM.
   * ---------------------------------------------------------------------- */

  // The object URL for a locally chosen file, so the photo previews without
  // uploading anything. Tracked at module scope (not on the element) so it
  // survives the element being swapped away and can still be revoked.
  var replyObjectUrl = null;

  function releaseReplyObjectUrl() {
    if (replyObjectUrl) {
      URL.revokeObjectURL(replyObjectUrl);
      replyObjectUrl = null;
    }
  }

  function paintReplyPreview() {
    var root = document.querySelector("[data-reply-preview]");
    if (!root) return;

    var stage = root.querySelector("[data-reply-preview-stage]");
    var empty = root.querySelector("[data-reply-preview-empty]");
    var imageEl = root.querySelector("[data-reply-preview-image]");
    var textEl = root.querySelector("[data-reply-preview-text]");

    var bodyField = document.getElementById("reply-body");
    var fileField = document.getElementById("reply-image");
    var removeField = document.querySelector('input[name="remove_image"]');

    var text = bodyField ? bodyField.value.trim() : "";

    // Which image wins: a file just chosen beats the stored one, and ticking
    // "Quitar la imagen actual" drops the stored one entirely.
    var src = "";
    var chosen = fileField && fileField.files && fileField.files[0];
    if (chosen) {
      src = replyObjectUrl || "";
    } else if (!(removeField && removeField.checked)) {
      src = imageEl ? imageEl.getAttribute("data-current-src") || "" : "";
    }

    if (imageEl) {
      if (src) {
        if (imageEl.getAttribute("src") !== src) imageEl.setAttribute("src", src);
        imageEl.hidden = false;
      } else {
        imageEl.removeAttribute("src");
        imageEl.hidden = true;
      }
    }

    if (textEl) {
      textEl.textContent = text;   // textContent, never innerHTML -- agent input
      textEl.hidden = text === "";
    }

    // Nothing to show yet: keep the frame, swap the bubble for the hint.
    var hasSomething = Boolean(text || src);
    if (stage) stage.hidden = !hasSomething;
    if (empty) empty.hidden = hasSomething;
  }

  document.addEventListener("input", function (event) {
    if (event.target.id === "reply-body") paintReplyPreview();
  });

  document.addEventListener("change", function (event) {
    var target = event.target;
    if (target.id === "reply-image") {
      releaseReplyObjectUrl();
      var file = target.files && target.files[0];
      // Only build a URL for something the browser can actually render; the
      // server still does the real type/size validation on submit.
      if (file && file.type.indexOf("image/") === 0) {
        replyObjectUrl = URL.createObjectURL(file);
      }
      paintReplyPreview();
    } else if (target.name === "remove_image") {
      paintReplyPreview();
    }
  });

  // The form arrives (and re-arrives, after a rejected save) by htmx swap, so
  // paint once it lands -- an edit drawer opens with values already in it.
  document.body.addEventListener("htmx:afterSwap", function (event) {
    if (event.target && event.target.id === "reply-modal-body") {
      releaseReplyObjectUrl();
      paintReplyPreview();
    }
  });

  // Closing the drawer drops the preview along with the form it described.
  document.addEventListener("close", function (event) {
    if (event.target && event.target.id === "reply-modal") releaseReplyObjectUrl();
  }, true);

  /* -------------------------------------------------------------------------
   * Server-error banner: the one thing every hx-post in this app was missing.
   *
   * A form that fails *validation* re-renders with its own inline messages --
   * that is a normal, successful HTTP response and needs nothing here. This
   * is for the other case: the request never got a body back at all (a 500,
   * a network drop), so the target never swaps and the screen just... sits
   * there. Until this, that looked identical to "nothing happened" -- click
   * Guardar, no error, no save, no clue. A quick reply's image upload hit
   * exactly this against a misconfigured storage backend: every attempt 500'd
   * server-side and the modal simply stayed open with what was typed, giving
   * no sign anything had gone wrong.
   *
   * Deliberately generic and content-free: the response body for a 500 is a
   * Django error page, not something to show an agent. This only answers
   * "did it work", which is the one thing its absence was hiding.
   * ---------------------------------------------------------------------- */

  var errorBannerTimer = null;

  function showErrorBanner(message) {
    var banner = document.querySelector("[data-error-banner]");
    if (!banner) {
      banner = document.createElement("div");
      banner.className = "error-banner";
      banner.setAttribute("data-error-banner", "");
      banner.setAttribute("role", "alert");
      var text = document.createElement("span");
      text.className = "error-banner__text";
      banner.appendChild(text);
      var close = document.createElement("button");
      close.type = "button";
      close.className = "error-banner__close";
      close.setAttribute("aria-label", "Cerrar aviso");
      close.textContent = "×";
      close.addEventListener("click", function () { hideErrorBanner(); });
      banner.appendChild(close);
      document.body.appendChild(banner);
    }
    banner.querySelector(".error-banner__text").textContent = message;
    banner.hidden = false;
    if (errorBannerTimer) clearTimeout(errorBannerTimer);
    errorBannerTimer = setTimeout(hideErrorBanner, 8000);
  }

  function hideErrorBanner() {
    var banner = document.querySelector("[data-error-banner]");
    if (banner) banner.hidden = true;
    if (errorBannerTimer) { clearTimeout(errorBannerTimer); errorBannerTimer = null; }
  }

  // A response in the 4xx/5xx range that htmx therefore did NOT swap in.
  // (A 422 IS swapped -- see the beforeSwap handler above -- so it never
  // reaches here; that path already shows its own inline field errors.)
  document.addEventListener("htmx:responseError", function (event) {
    var status = event.detail.xhr.status;
    showErrorBanner(
      status >= 500
        ? "No se pudo guardar: ocurrió un error en el servidor. Inténtalo de nuevo."
        : "No se pudo completar la acción (" + status + "). Inténtalo de nuevo."
    );
  });

  // The request never reached the server at all -- offline, DNS, CORS.
  document.addEventListener("htmx:sendError", function () {
    showErrorBanner("No se pudo conectar. Revisa tu conexión e inténtalo de nuevo.");
  });
})();
