// Harvey AI website chat. Options, as attributes on the script tag:
// data-name, data-greeting, data-color, data-position ("right" or "left").
(function () {
  var script = document.currentScript;
  var options = script.dataset;
  var endpoint = new URL("chat", script.src).href;
  var name = options.name || "Harvey AI";
  var color = options.color || "#1f2937";
  var side = options.position === "left" ? "left" : "right";
  var greeting = options.greeting ||
    "Hi, I'm " + name + ", an AI assistant. Ask me anything. If you share your email, someone may follow up.";
  var session = "";

  var host = document.createElement("div");
  var root = host.attachShadow({ mode: "open" });
  var style = document.createElement("style");
  style.textContent =
    ":host{all:initial;font:14px/1.45 system-ui,sans-serif}" +
    "button{font:inherit;cursor:pointer;border:0;border-radius:8px;color:#fff;background:var(--c)}" +
    "button:focus-visible,input:focus-visible{outline:2px solid var(--c);outline-offset:2px}" +
    ".open{position:fixed;bottom:20px;SIDE:20px;padding:12px 16px;z-index:2147483000;box-shadow:0 4px 14px #0004}" +
    ".panel{position:fixed;bottom:76px;SIDE:20px;width:min(360px,calc(100vw - 40px));height:min(480px,70vh);z-index:2147483000;" +
    "display:flex;flex-direction:column;background:#fff;color:#111;border-radius:12px;box-shadow:0 8px 30px #0005;overflow:hidden}" +
    ".panel[hidden]{display:none}" +
    "header{padding:12px 14px;color:#fff;background:var(--c);font-weight:600}" +
    ".log{flex:1;overflow-y:auto;padding:12px;display:flex;flex-direction:column;gap:8px}" +
    ".msg{max-width:85%;padding:8px 10px;border-radius:10px;background:#f1f2f4;white-space:pre-wrap;overflow-wrap:anywhere}" +
    ".me{align-self:flex-end;color:#fff;background:var(--c)}" +
    "form{display:flex;gap:6px;padding:10px;border-top:1px solid #e5e7eb}" +
    "input{flex:1;min-width:0;font:inherit;padding:8px;border:1px solid #d1d5db;border-radius:8px}" +
    "form button{padding:8px 12px}";
  style.textContent = style.textContent.replace(/SIDE/g, side);
  host.style.setProperty("--c", color);

  var open = document.createElement("button");
  open.className = "open";
  open.textContent = "Chat with " + name;
  open.setAttribute("aria-expanded", "false");

  var panel = document.createElement("section");
  panel.className = "panel";
  panel.hidden = true;
  panel.setAttribute("aria-label", name + " chat");
  var header = document.createElement("header");
  header.textContent = name;
  var log = document.createElement("div");
  log.className = "log";
  log.setAttribute("role", "log");
  log.setAttribute("aria-live", "polite");
  var form = document.createElement("form");
  var input = document.createElement("input");
  input.maxLength = 1000;
  input.required = true;
  input.setAttribute("aria-label", "Your message");
  input.placeholder = "Type a message";
  var send = document.createElement("button");
  send.textContent = "Send";
  form.append(input, send);
  panel.append(header, log, form);
  root.append(style, open, panel);

  function add(text, mine) {
    var line = document.createElement("div");
    line.className = mine ? "msg me" : "msg";
    line.textContent = text; // never innerHTML: replies are model output
    log.append(line);
    log.scrollTop = log.scrollHeight;
  }

  open.addEventListener("click", function () {
    panel.hidden = !panel.hidden;
    open.setAttribute("aria-expanded", String(!panel.hidden));
    if (!panel.hidden) input.focus();
  });

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    var message = input.value.trim();
    if (!message) return;
    input.value = "";
    add(message, true);
    send.disabled = true;
    fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: message, session: session })
    }).then(function (response) {
      if (!response.ok) throw new Error(String(response.status));
      return response.json();
    }).then(function (data) {
      session = data.session;
      add(data.reply, false);
    }).catch(function () {
      add("Sorry, I couldn't reach the server. Please try again shortly.", false);
    }).finally(function () {
      send.disabled = false;
      input.focus();
    });
  });

  add(greeting, false);
  document.body.append(host);
})();
