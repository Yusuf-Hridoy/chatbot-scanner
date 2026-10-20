// Widget lab helpers (fictional Lumen Skincare). Plain JavaScript, no dependencies.
// Each lab page copies one hard pattern from real chat widgets. Replies come from the demo bot's /chat.
(function () {
  "use strict";

  // Real sites set cookies and storage. The debug output of `audit check` must never contain them.
  document.cookie = "lumen_lab_visit=LAB-COOKIE-7Q2K; path=/; SameSite=Lax";
  try {
    localStorage.setItem("lumen_lab_token", "LAB-STORAGE-9X4M");
  } catch (error) {
    // storage blocked: nothing to do
  }

  // A cookie banner, like most real shops (not inside the chat iframe). The generic preset clicks "Accept".
  if (!document.body.classList.contains("lab-frame-page")) {
    var banner = document.createElement("div");
    banner.className = "lab-cookies";
    banner.innerHTML = '<p>We use cookies to improve your visit.</p><button type="button">Accept</button>';
    banner.querySelector("button").addEventListener("click", function () { banner.remove(); });
    document.body.appendChild(banner);
  }

  function sleep(ms) {
    return new Promise(function (resolve) { setTimeout(resolve, ms); });
  }

  function sessionId() {
    var key = "lumen_lab_session";
    var id = null;
    try {
      id = sessionStorage.getItem(key);
      if (!id) {
        id = "lab-" + Date.now() + "-" + Math.random().toString(16).slice(2);
        sessionStorage.setItem(key, id);
      }
    } catch (error) {
      id = id || "lab-no-storage";
    }
    return id;
  }

  async function ask(message) {
    try {
      var response = await fetch("/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: message, session_id: sessionId() })
      });
      if (response.ok) {
        var data = await response.json();
        var reply = data && data.data && data.data.reply;
        if (typeof reply === "string") return reply;
      }
    } catch (error) {
      // fall through to the error text
    }
    return "Sorry, something went wrong.";
  }

  var DEFAULT_LAUNCHER = '<button type="button" class="lab-launcher" aria-label="Open chat">Chat with us</button>';
  var DEFAULT_INPUT = '<textarea class="lab-input" rows="2" aria-label="Message" placeholder="Type your message"></textarea>';

  // Builds the launcher and the chat panel inside `root` (document.body or a shadow root).
  // options: launcher (false = none), launcherHtml, inputHtml, open (panel visible from the start).
  function mount(root, options) {
    options = options || {};
    var box = document.createElement("div");
    box.className = "lab-widget";
    box.innerHTML =
      (options.launcher === false ? "" : options.launcherHtml || DEFAULT_LAUNCHER) +
      '<section class="lab-panel"' + (options.open ? "" : " hidden") + ">" +
      '<header class="lab-header">Lumen Skincare chat</header>' +
      '<div class="lab-messages"></div>' +
      '<div class="lab-typing" hidden>Lumen is typing</div>' +
      '<form class="lab-form">' + (options.inputHtml || DEFAULT_INPUT) +
      '<button type="submit" class="lab-send">Send</button></form>' +
      "</section>";
    root.appendChild(box);
    function find(selector) { return box.querySelector(selector); }
    return {
      launcher: find(".lab-launcher, .lab-bubble-icon"),
      panel: find(".lab-panel"),
      messages: find(".lab-messages"),
      typing: find(".lab-typing"),
      form: find(".lab-form"),
      input: find(".lab-input"),
      send: find(".lab-send")
    };
  }

  // Shows the panel when the launcher is clicked. onOpen runs once, the first time.
  function openOnClick(chat, onOpen) {
    chat.launcher.addEventListener("click", function () {
      if (!chat.panel.hidden) return;
      chat.panel.hidden = false;
      chat.input.focus();
      if (onOpen) onOpen();
    });
  }

  function addBubble(chat, role, text) {
    var bubble = document.createElement("div");
    bubble.className = "msg " + (role === "bot" ? "msg-bot" : "msg-user");
    bubble.textContent = text;
    chat.messages.appendChild(bubble);
    chat.messages.scrollTop = chat.messages.scrollHeight;
    return bubble;
  }

  function readInput(chat) {
    var isField = "value" in chat.input;
    var text = isField ? chat.input.value : chat.input.textContent;
    if (isField) chat.input.value = "";
    else chat.input.textContent = "";
    return text.trim();
  }

  // Sends with the Send button and (unless enterSends is false) the Enter key.
  // Returns send(message), so pages can send text from other controls too.
  function onSubmit(chat, handler, options) {
    options = options || {};
    var busy = false;
    async function send(message) {
      if (!message || busy) return;
      busy = true;
      try {
        await handler(message);
      } finally {
        busy = false;
      }
    }
    chat.form.addEventListener("submit", function (event) {
      event.preventDefault();
      send(readInput(chat));
    });
    chat.input.addEventListener("keydown", function (event) {
      if (event.key !== "Enter" || event.shiftKey) return;
      event.preventDefault();
      if (options.enterSends !== false) send(readInput(chat));
    });
    return send;
  }

  // The usual turn: the customer's message, "typing", then the bot's reply.
  // options: delayMs (minimum time before the reply), afterCustomer(), render(bubble, reply).
  async function converse(chat, message, options) {
    options = options || {};
    addBubble(chat, "user", message);
    if (options.afterCustomer) options.afterCustomer();
    chat.typing.hidden = false;
    var started = Date.now();
    var reply = await ask(message);
    var left = (options.delayMs || 300) - (Date.now() - started);
    if (left > 0) await sleep(left);
    chat.typing.hidden = true;
    var bubble = addBubble(chat, "bot", reply);
    if (options.render) options.render(bubble, reply);
    return bubble;
  }

  window.LumenLab = {
    sleep: sleep,
    ask: ask,
    mount: mount,
    openOnClick: openOnClick,
    addBubble: addBubble,
    onSubmit: onSubmit,
    converse: converse
  };
})();
