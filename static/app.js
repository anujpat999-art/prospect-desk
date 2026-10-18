(function () {
  var $ = function (id) { return document.getElementById(id); };
  var form = $("f"), card = $("card"), errors = $("errors"), go = $("go");
  var fields = ["name", "role", "company", "industry", "size", "source", "last_contact_days", "context"];
  var emptyHtml = card.innerHTML, timer = null, seq = 0;

  function payload() {
    var p = {};
    fields.forEach(function (k) { p[k] = $(k).value; });
    return p;
  }
  function showErrors(list) {
    errors.textContent = "";
    var b = document.createElement("b");
    b.textContent = "Fix these before saving:";
    var ul = document.createElement("ul");
    list.forEach(function (m) { var li = document.createElement("li"); li.textContent = m; ul.appendChild(li); });
    errors.appendChild(b); errors.appendChild(ul);
    errors.hidden = false;
    errors.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
  function post(url, body) {
    return fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
      .then(function (r) { return r.json(); });
  }

  // live scorecard: the server runs the same logic layer it uses when saving. No AI call, nothing stored.
  function preview() {
    var my = ++seq;
    post("/api/preview", payload()).then(function (d) {
      if (my !== seq) return;
      if (!d.ok) { card.innerHTML = emptyHtml; return; }
      card.innerHTML = d.empty ? emptyHtml : d.html;   // html is rendered and escaped by the server template
    }).catch(function () {});
  }
  function schedule() { clearTimeout(timer); timer = setTimeout(preview, 250); }
  fields.forEach(function (k) { $(k).addEventListener("input", schedule); $(k).addEventListener("change", schedule); });
  $("context").addEventListener("input", function () { $("count").textContent = this.value.length; });

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    errors.hidden = true; go.disabled = true; go.textContent = "Scoring and writing…";
    post("/api/leads", payload()).then(function (d) {
      if (d.ok) { location.href = "/lead/" + d.id; return; }
      showErrors(d.errors || ["Something went wrong. Try again."]);
    }).catch(function () {
      showErrors(["Could not reach the server. Check your connection and try again."]);
    }).then(function () { go.disabled = false; go.textContent = "Save lead and write outreach"; });
  });

  $("example").addEventListener("click", function () {
    var ex = { name: "Priya Nair", role: "Head of Sales", company: "Finlytics", industry: "Fintech", size: "51-200",
      source: "demo_request", last_contact_days: "1",
      context: "Asked for a demo and pricing after our webinar. Budget approved this quarter. They are comparing us with one other vendor because manual lead follow-up is too slow." };
    fields.forEach(function (k) { $(k).value = ex[k]; });
    $("count").textContent = ex.context.length;
    preview();
  });

  $("import").addEventListener("click", function () {
    var msg = $("import-msg"), btn = this;
    msg.textContent = "Importing…"; btn.disabled = true;
    post("/api/batch", { csv: $("csv").value }).then(function (d) {
      if (d.ok) {
        msg.textContent = "";
        var a = document.createElement("a");
        a.href = "/leads"; a.textContent = "Open the pipeline";
        msg.append("Scored " + d.saved + " lead" + (d.saved === 1 ? "" : "s") + (d.problems.length ? ", skipped " + d.problems.length + " (" + d.problems[0] + ")" : "") + ". ", a);
      } else { msg.textContent = (d.errors || ["Import failed."]).join(" "); }
    }).catch(function () { msg.textContent = "Could not reach the server."; })
      .then(function () { btn.disabled = false; });
  });
})();
