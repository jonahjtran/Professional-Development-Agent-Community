// Polls for open questions, shows a count in the tab title and header, and (if allowed) raises a
// browser notification for each new question. It only talks to this same local server.
(function () {
  var seen = null;
  var button = document.getElementById("notify");
  var badge = document.getElementById("badge");
  var base = document.title.replace(/^\(\d+\) /, "");

  if ("Notification" in window && Notification.permission === "default") {
    button.hidden = false;
    button.onclick = function () {
      Notification.requestPermission().then(function () { button.hidden = true; });
    };
  }

  function check() {
    fetch("/api/pending").then(function (r) { return r.json(); }).then(function (items) {
      document.title = (items.length ? "(" + items.length + ") " : "") + base;
      badge.textContent = items.length ? "(" + items.length + ")" : "";
      var ids = items.map(function (i) { return i.id; });
      if (seen !== null && "Notification" in window && Notification.permission === "granted") {
        items.forEach(function (item) {
          if (seen.indexOf(item.id) === -1) {
            var n = new Notification("Agent Studio needs you", { body: item.project + ": " + item.title });
            n.onclick = function () { window.focus(); location.href = item.url; };
          }
        });
      }
      seen = ids;
    }).catch(function () {});
  }
  check();
  setInterval(check, 5000);
})();
