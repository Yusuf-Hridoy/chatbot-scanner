// Lumen Skincare demo chat widget (fictional). Plain JavaScript, no dependencies.
// The same file runs on the shop page (cookie banner + launcher) and inside the chat panel.
(function () {
  "use strict";

  var WORD_DELAY_MS = 30;
  var BUBBLE_GAP_MS = 150;
  var SPLIT_AFTER_WORDS = 12;

  function sleep(ms) {
    return new Promise(function (resolve) { setTimeout(resolve, ms); });
  }

  function newId() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    return "s-" + Date.now() + "-" + Math.random().toString(16).slice(2);
  }

  // ---- shop page: cookie banner and launcher ----
  var banner = document.querySelector("[data-cookie-banner]");
  var accept = document.querySelector("[data-cookie-accept]");
  if (banner && accept) {
    accept.addEventListener("click", function () { banner.hidden = true; });
  }

  var launcher = document.querySelector("[data-chat-launcher]");
  if (launcher) {
    launcher.addEventListener("click", function () {
      var container = document.querySelector("[data-chat-frame]") || document.querySelector("[data-chat-inline]");
      if (container) {
        container.hidden = false;
        var input = document.querySelector("[data-chat-inline] [data-chat-input]");
        if (input) input.focus();
      }
    });
  }

  // ---- chat panel (inside the iframe, or inline when ?frame=0) ----
  var panel = document.querySelector("[data-chat-panel]");
  if (!panel) return;

  var messages = panel.querySelector("[data-chat-messages]");
  var typing = panel.querySelector("[data-chat-typing]");
  var form = panel.querySelector("[data-chat-form]");
  var input = panel.querySelector("[data-chat-input]");
  var busy = false;

  function sessionId() {
    var key = "lumen_chat_session";
    var id = sessionStorage.getItem(key);
    if (!id) {
      id = newId();
      sessionStorage.setItem(key, id);
    }
    return id;
  }

  function addBubble(role) {
    var bubble = document.createElement("div");
    bubble.className = "bubble " + (role === "bot" ? "bot-message" : "customer-message");
    bubble.setAttribute("data-chat-role", role);
    messages.appendChild(bubble);
    messages.scrollTop = messages.scrollHeight;
    return bubble;
  }

  async function streamInto(bubble, text) {
    var words = text.split(" ");
    for (var i = 0; i < words.length; i++) {
      bubble.textContent += (i ? " " : "") + words[i];
      messages.scrollTop = messages.scrollHeight;
      await sleep(WORD_DELAY_MS);
    }
  }

  function splitReply(text) {
    // Long replies arrive as two bubbles, split at the first sentence end.
    if (text.split(" ").length <= SPLIT_AFTER_WORDS) return [text];
    var match = text.match(/^([\s\S]+?[.!?])\s+([\s\S]+)$/);
    return match ? [match[1], match[2]] : [text];
  }

  async function send(message) {
    busy = true;
    addBubble("customer").textContent = message;
    typing.hidden = false;
    await sleep(400 + Math.floor(Math.random() * 400));
    var reply = null;
    try {
      var response = await fetch("/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: message, session_id: sessionId() })
      });
      if (response.ok) {
        var data = await response.json();
        reply = data && data.data && data.data.reply;
      }
    } catch (error) {
      reply = null;
    }
    typing.hidden = true;
    if (typeof reply !== "string") {
      var failed = addBubble("bot");
      failed.setAttribute("data-chat-error", "");
      failed.textContent = "Sorry, something went wrong.";
    } else {
      var parts = splitReply(reply);
      for (var i = 0; i < parts.length; i++) {
        if (i) await sleep(BUBBLE_GAP_MS);
        await streamInto(addBubble("bot"), parts[i]);
      }
    }
    busy = false;
  }

  function submit() {
    var message = input.value.trim();
    if (!message || busy) return;
    input.value = "";
    send(message);
  }

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    submit();
  });
  input.addEventListener("keydown", function (event) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      submit();
    }
  });
})();
