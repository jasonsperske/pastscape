/* ==========================================================================
   Pastscape Messenger - client for a static mail archive.

   No framework, no build step, no server. Folder listings and the search
   index are plain JSON fetched on demand; message bodies are the published
   HTML pages, parsed out of the document we would have navigated to anyway.
   That keeps exactly one copy of every message on disk and means the archive
   still works with scripting off.
   ========================================================================== */
(function () {
  "use strict";

  // Row tuple layout, mirrored in render.py
  var R_UID = 0, R_SUBJ = 1, R_FNAME = 2, R_FADDR = 3, R_DATE = 4,
      R_PRIO = 5, R_FLAGS = 6, R_THREAD = 7, R_SIZE = 8;
  var FLAG_ATTACH = 1, FLAG_FLAGGED = 2, FLAG_UNREAD = 4;

  // Search field bits, mirrored in search.py
  var F_SUBJECT = 1, F_SENDER = 2, F_RECIPIENT = 4, F_BODY = 8, F_ATTACH = 16;

  var ROW_H = 16;
  var LS_KEY = "pastscape.read.v1";

  var app = {
    cfg: null,
    folders: [],
    byslug: {},
    folder: null,
    rows: [],            // rows of the current folder, after sort/thread
    flatRows: [],        // display order (thread-aware)
    sortCol: "date",
    sortDir: -1,
    threaded: false,
    unreadOnly: false,
    selected: -1,
    listCache: {},
    searchMeta: null,
    shardCache: {},
    docs: null,
    collapsed: {},
    read: {},
    lastQuery: "",
    results: [],
    resultSel: -1
  };

  // ---------------------------------------------------------------- helpers

  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function icon(id, cls) {
    var svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    if (cls) svg.setAttribute("class", cls);
    var use = document.createElementNS("http://www.w3.org/2000/svg", "use");
    use.setAttribute("href", "#" + id);
    svg.appendChild(use);
    return svg;
  }

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function pad(n) { return n < 10 ? "0" + n : "" + n; }

  /* Communicator showed "6/2/97 4:00 PM" -- short, ambiguous, and exactly
     what we want here. */
  function fmtDate(epoch) {
    if (!epoch) return "";
    var d = new Date(epoch * 1000);
    var h = d.getHours(), ap = h >= 12 ? "PM" : "AM";
    h = h % 12; if (h === 0) h = 12;
    return (d.getMonth() + 1) + "/" + d.getDate() + "/" + pad(d.getFullYear() % 100) +
           " " + h + ":" + pad(d.getMinutes()) + " " + ap;
  }

  function fmtDateLong(epoch) {
    if (!epoch) return "(no date)";
    var d = new Date(epoch * 1000);
    var days = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
    var mon = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
    return days[d.getDay()] + ", " + pad(d.getDate()) + " " + mon[d.getMonth()] + " " +
           d.getFullYear() + " " + pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
  }

  function fmtSize(bytes) {
    if (!bytes) return "";
    if (bytes < 1024) return bytes + "B";
    if (bytes < 1024 * 1024) return Math.round(bytes / 1024) + "K";
    return (bytes / 1048576).toFixed(1) + "M";
  }

  function pageFor(uid) { return "msg/" + uid.slice(0, 2) + "/" + uid + ".html"; }

  function status(text) { var n = $("#ps-status-text"); if (n) n.textContent = text; }

  /* Record what we set so the hashchange listener can tell our own navigation
     apart from the reader pressing Back or pasting a link. */
  function setHash(value) {
    app.selfHash = value;
    location.hash = "#" + value;
  }

  var progressTimer = null;
  function progress(pct) {
    var bar = $("#ps-progress-bar");
    if (!bar) return;
    bar.style.width = Math.max(0, Math.min(100, pct)) + "%";
    if (pct >= 100) {
      clearTimeout(progressTimer);
      progressTimer = setTimeout(function () { bar.style.width = "0"; }, 350);
    }
  }

  function getJSON(url) {
    return fetch(url, { cache: "no-cache" }).then(function (r) {
      if (!r.ok) throw new Error(url + ": HTTP " + r.status);
      return r.json();
    });
  }

  // ------------------------------------------------------------- read state

  function loadRead() {
    try { app.read = JSON.parse(localStorage.getItem(LS_KEY) || "{}"); }
    catch (e) { app.read = {}; }
  }
  var saveTimer = null;
  function saveRead() {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(function () {
      try { localStorage.setItem(LS_KEY, JSON.stringify(app.read)); } catch (e) { /* full or blocked */ }
    }, 400);
  }
  function isUnread(row) {
    if (app.read[row[R_UID]]) return false;
    return (row[R_FLAGS] & FLAG_UNREAD) !== 0;
  }

  // ============================================================== folder tree

  function folderIcon(f) {
    var name = f.name.toLowerCase();
    if (f.depth > 0) return "ic-folder";
    var map = {
      "inbox": "ic-inbox", "unsent messages": "ic-outbox", "drafts": "ic-drafts",
      "templates": "ic-templates", "sent": "ic-sent", "trash": "ic-trash",
      "junk": "ic-junk", "archive": "ic-archive", "samples": "ic-folder"
    };
    return map[name] || "ic-folder";
  }

  function renderTree() {
    var host = $("#ps-tree");
    host.textContent = "";

    var head = el("div", "ps-tree-row ps-tree-group");
    head.appendChild(el("span", "twisty", "−"));
    head.appendChild(icon("ic-localmail", "ficon"));
    head.appendChild(el("span", "fname", app.cfg.title || "Local Mail"));
    host.appendChild(head);

    app.folders.forEach(function (f) {
      var row = el("div", "ps-tree-row");
      row.style.paddingLeft = (10 + f.depth * 14) + "px";
      row.dataset.slug = f.slug;
      if (f.unread > 0) row.classList.add("unread");
      if (app.folder && app.folder.slug === f.slug) row.classList.add("selected");

      var hasKids = app.folders.some(function (o) { return o.parent === f.path; });
      var tw = el("span", "twisty" + (hasKids ? " has-kids" : ""), hasKids ? "−" : "");
      row.appendChild(tw);
      row.appendChild(icon(folderIcon(f), "ficon"));
      row.appendChild(el("span", "fname", f.name));
      if (f.unread > 0) row.appendChild(el("span", "fcount", "(" + f.unread + ")"));
      row.addEventListener("click", function () { selectFolder(f.slug); });
      host.appendChild(row);
    });

    if (app.cfg.newsHost) {
      var news = el("div", "ps-tree-row ps-tree-group");
      news.style.marginTop = "4px";
      news.appendChild(el("span", "twisty", "+"));
      news.appendChild(icon("ic-news", "ficon"));
      news.appendChild(el("span", "fname", app.cfg.newsHost));
      host.appendChild(news);
    }
  }

  // ============================================================ message list

  function selectFolder(slug, keepHash) {
    var f = app.byslug[slug];
    if (!f) return;
    app.folder = f;
    app.selected = -1;
    renderTree();
    $("#ps-folder-title").textContent = f.path;
    document.title = "Pastscape - " + f.path;
    status("Opening folder " + f.path + "…");
    progress(30);

    loadFolderRows(f).then(function (rows) {
      app.rows = rows;
      progress(80);
      applyView();
      progress(100);
      status(f.count + " message" + (f.count === 1 ? "" : "s") + " in " + f.path);
      if (!keepHash) setHash("folder=" + slug);
      showNoMessage();
    }).catch(function (err) {
      status("Error: " + err.message);
      progress(100);
    });
  }

  function loadFolderRows(f) {
    if (app.listCache[f.slug]) return Promise.resolve(app.listCache[f.slug]);
    return getJSON(f.file).then(function (data) {
      app.listCache[f.slug] = data.rows || [];
      return app.listCache[f.slug];
    });
  }

  function compareRows(a, b) {
    var d = 0;
    switch (app.sortCol) {
      case "subject": d = normSubj(a[R_SUBJ]).localeCompare(normSubj(b[R_SUBJ])); break;
      case "sender": d = (a[R_FNAME] || a[R_FADDR] || "").localeCompare(b[R_FNAME] || b[R_FADDR] || ""); break;
      case "priority": d = prioRank(a[R_PRIO]) - prioRank(b[R_PRIO]); break;
      case "size": d = (a[R_SIZE] || 0) - (b[R_SIZE] || 0); break;
      default: d = (a[R_DATE] || 0) - (b[R_DATE] || 0);
    }
    if (d === 0) d = (a[R_DATE] || 0) - (b[R_DATE] || 0);
    return d * app.sortDir;
  }

  function normSubj(s) { return String(s || "").replace(/^\s*((re|fwd?|aw|sv)\s*:\s*)+/i, "").toLowerCase(); }
  function prioRank(p) {
    return { "Highest": 5, "High": 4, "Normal": 3, "Low": 2, "Lowest": 1 }[p] || 0;
  }

  /* Threaded view: group by thread key, order groups by their newest member,
     then indent replies under the first message of the group. */
  function buildFlatRows() {
    var rows = app.rows.filter(function (r) { return !app.unreadOnly || isUnread(r); });
    var out = [];
    if (!app.threaded) {
      rows = rows.slice().sort(compareRows);
      rows.forEach(function (r) { out.push({ row: r, depth: 0, kids: 0, key: null }); });
      app.flatRows = out;
      return;
    }
    var groups = {}, order = [];
    rows.forEach(function (r) {
      var k = r[R_THREAD] || r[R_UID];
      if (!groups[k]) { groups[k] = []; order.push(k); }
      groups[k].push(r);
    });
    order.forEach(function (k) { groups[k].sort(function (a, b) { return (a[R_DATE] || 0) - (b[R_DATE] || 0); }); });
    order.sort(function (ka, kb) {
      var a = groups[ka][groups[ka].length - 1], b = groups[kb][groups[kb].length - 1];
      return compareRows(a, b);
    });
    order.forEach(function (k) {
      var g = groups[k];
      out.push({ row: g[0], depth: 0, kids: g.length - 1, key: k });
      if (!app.collapsed[k]) {
        for (var i = 1; i < g.length; i++) out.push({ row: g[i], depth: 1, kids: 0, key: k });
      }
    });
    app.flatRows = out;
  }

  function applyView() {
    buildFlatRows();
    updateSortMarks();
    renderList();
  }

  function updateSortMarks() {
    $$("#ps-list-head .col").forEach(function (c) {
      var mark = $(".sortmark", c);
      if (!mark) return;
      mark.innerHTML = "";
      if (c.dataset.col === app.sortCol) {
        mark.appendChild(icon(app.sortDir > 0 ? "ic-sortasc" : "ic-sortdesc"));
      }
    });
  }

  /* Virtualised: an archive can hold six figures of messages and a real
     Pentium-era client would not have rendered them all either. */
  function renderList() {
    var scroll = $("#ps-list-scroll");
    var host = $("#ps-list");
    var total = app.flatRows.length;

    if (!total) {
      host.style.height = "auto";
      host.textContent = "";
      host.appendChild(el("div", "ps-empty", app.unreadOnly ? "No unread messages." : "This folder is empty."));
      return;
    }
    host.style.height = (total * ROW_H) + "px";
    host.style.position = "relative";

    var top = scroll.scrollTop;
    var first = Math.max(0, Math.floor(top / ROW_H) - 8);
    var visible = Math.ceil(scroll.clientHeight / ROW_H) + 16;
    var last = Math.min(total, first + visible);

    var frag = document.createDocumentFragment();
    for (var i = first; i < last; i++) frag.appendChild(buildRow(i));
    host.textContent = "";
    host.appendChild(frag);
  }

  function buildRow(i) {
    var entry = app.flatRows[i];
    var r = entry.row;
    var node = el("div", "ps-row");
    node.style.position = "absolute";
    node.style.top = (i * ROW_H) + "px";
    node.style.left = "0";
    node.style.right = "0";
    node.dataset.index = i;
    node.dataset.uid = r[R_UID];
    if (isUnread(r)) node.classList.add("unread");
    if (i === app.selected) node.classList.add("selected");

    var cw = columnWidths();

    var c0 = el("div", "cell c-icon");
    c0.style.width = cw.subject + "px";
    c0.style.flex = "0 0 " + cw.subject + "px";
    c0.style.paddingLeft = (2 + entry.depth * 14) + "px";
    if (entry.kids > 0) {
      var tog = el("span", "thread-toggle", app.collapsed[entry.key] ? "+" : "−");
      tog.addEventListener("click", function (ev) {
        ev.stopPropagation();
        app.collapsed[entry.key] = !app.collapsed[entry.key];
        applyView();
      });
      c0.appendChild(tog);
    } else if (app.threaded) {
      c0.appendChild(el("span", "thread-spacer"));
    }
    c0.appendChild(icon(isUnread(r) ? "ic-mail-unread" : "ic-mail-read"));
    c0.appendChild(el("span", "t", r[R_SUBJ] || "(no subject)"));
    if (r[R_FLAGS] & FLAG_ATTACH) c0.appendChild(icon("ic-attach"));
    node.appendChild(c0);

    var c1 = el("div", "cell c-flag");
    c1.style.flex = "0 0 18px";
    if (r[R_FLAGS] & FLAG_FLAGGED) c1.appendChild(icon("ic-flag"));
    node.appendChild(c1);

    var c2 = el("div", "cell c-sender", r[R_FNAME] || r[R_FADDR] || "(unknown)");
    c2.style.flex = "0 0 " + cw.sender + "px";
    c2.title = r[R_FADDR] || "";
    node.appendChild(c2);

    var c3 = el("div", "cell c-date", fmtDate(r[R_DATE]));
    c3.style.flex = "0 0 " + cw.date + "px";
    node.appendChild(c3);

    var c4 = el("div", "cell c-prio");
    c4.style.flex = "1 1 " + cw.prio + "px";
    if (r[R_PRIO]) {
      if (r[R_PRIO] === "High" || r[R_PRIO] === "Highest") c4.appendChild(icon("ic-priority"));
      c4.appendChild(el("span", null, r[R_PRIO]));
    }
    node.appendChild(c4);

    node.addEventListener("click", function () { selectRow(i); });
    node.addEventListener("dblclick", function () { openStandalone(r[R_UID]); });
    return node;
  }

  function columnWidths() {
    var head = $("#ps-list-head");
    return {
      subject: parseInt($('.col[data-col="subject"]', head).style.flexBasis, 10) || 260,
      sender: parseInt($('.col[data-col="sender"]', head).style.flexBasis, 10) || 150,
      date: parseInt($('.col[data-col="date"]', head).style.flexBasis, 10) || 110,
      prio: 80
    };
  }

  function selectRow(i, keepHash) {
    if (i < 0 || i >= app.flatRows.length) return;
    app.selected = i;
    renderList();
    var row = app.flatRows[i].row;
    var node = $('#ps-list .ps-row[data-index="' + i + '"]');
    if (node) node.scrollIntoView({ block: "nearest" });
    openMessage(row[R_UID], keepHash);
  }

  function scrollSelectedIntoView() {
    var scroll = $("#ps-list-scroll");
    var y = app.selected * ROW_H;
    if (y < scroll.scrollTop) scroll.scrollTop = y;
    else if (y + ROW_H > scroll.scrollTop + scroll.clientHeight) {
      scroll.scrollTop = y + ROW_H - scroll.clientHeight;
    }
  }

  // ========================================================== message pane

  var currentMeta = null;

  function showNoMessage() {
    currentMeta = null;
    var pane = $("#ps-msgpane");
    pane.textContent = "";
    pane.appendChild(el("div", "ps-nomsg", "No message selected."));
    updateToolbarState();
  }

  function openMessage(uid, keepHash) {
    var pane = $("#ps-msgpane");
    status("Reading message…");
    progress(40);
    return fetch(pageFor(uid), { cache: "no-cache" })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.text();
      })
      .then(function (html) {
        var doc = new DOMParser().parseFromString(html, "text/html");
        var article = doc.getElementById("ps-article");
        if (!article) throw new Error("message page has no article");
        var metaNode = article.querySelector("#ps-meta");
        currentMeta = metaNode ? JSON.parse(metaNode.textContent) : null;
        if (metaNode) metaNode.remove();

        pane.textContent = "";
        pane.appendChild(document.importNode(article, true));
        pane.scrollTop = 0;
        wireBody(pane);

        if (!app.read[uid]) { app.read[uid] = 1; saveRead(); markFolderRead(uid); }
        if (!keepHash) setHash("msg=" + uid);
        document.title = "Pastscape - " + (currentMeta ? currentMeta.subject || "(no subject)" : uid);
        status(currentMeta ? (currentMeta.subject || "(no subject)") : "Message loaded");
        progress(100);
        updateToolbarState();
        return currentMeta;
      })
      .catch(function (err) {
        pane.textContent = "";
        pane.appendChild(el("div", "ps-nomsg", "Could not load message: " + err.message));
        progress(100);
        status("Error loading message");
        return null;
      });
  }

  /* Deep link straight to a message: the page we just fetched knows which
     folder it lives in, so open that folder and highlight the row rather than
     leaving the list pane empty. */
  function revealMessage(uid) {
    return openMessage(uid, true).then(function (meta) {
      if (!meta) return;
      var f = app.folders.find(function (o) { return o.path === meta.folder; });
      if (!f) return;
      app.folder = f;
      renderTree();
      $("#ps-folder-title").textContent = f.path;
      return loadFolderRows(f).then(function (rows) {
        app.rows = rows;
        applyView();
        var idx = app.flatRows.findIndex(function (entry) { return entry.row[R_UID] === uid; });
        if (idx >= 0) {
          app.selected = idx;
          renderList();
          scrollSelectedIntoView();
        }
        status(f.count + " message" + (f.count === 1 ? "" : "s") + " in " + f.path);
      });
    });
  }

  function markFolderRead(uid) {
    if (!app.folder) return;
    var rows = app.listCache[app.folder.slug] || [];
    for (var i = 0; i < rows.length; i++) {
      if (rows[i][R_UID] === uid && (rows[i][R_FLAGS] & FLAG_UNREAD)) {
        app.folder.unread = Math.max(0, app.folder.unread - 1);
        renderTree();
        break;
      }
    }
    renderList();
  }

  function wireBody(root) {
    var bar = $(".ps-remote-bar", root);
    if (bar) {
      var btn = $("button", bar);
      if (btn) {
        btn.addEventListener("click", function () {
          $$("img[data-ps-src]", root).forEach(function (img) {
            img.src = img.getAttribute("data-ps-src");
            img.removeAttribute("data-ps-src");
            img.classList.remove("ps-blocked-img");
          });
          bar.remove();
        });
      }
    }
    $$("a[href]", root).forEach(function (a) {
      var href = a.getAttribute("href") || "";
      if (/^(https?|ftp):/i.test(href)) { a.target = "_blank"; a.rel = "noopener noreferrer nofollow"; }
    });
    var srcToggle = $(".ps-source-toggle", root);
    if (srcToggle) {
      srcToggle.addEventListener("click", function (ev) {
        ev.preventDefault();
        var box = $(".ps-source", root);
        if (box) box.style.display = box.style.display === "none" ? "block" : "none";
      });
    }
  }

  // ============================================================ mailto: verbs

  function addrOf(a) {
    if (!a) return "";
    return a.a || "";
  }

  function quotedBody(meta) {
    var who = meta.from && (meta.from.n || meta.from.a) ? (meta.from.n || meta.from.a) : "someone";
    var head = "\n\n" + who + " wrote:\n";
    var text = (meta.quote || "").split("\n").slice(0, 40)
      .map(function (l) { return "> " + l; }).join("\n");
    return head + text;
  }

  function mailto(kind) {
    if (!currentMeta) { status("Select a message first."); return; }
    var m = currentMeta;
    var to = [], cc = [], subject = m.subject || "";

    if (kind === "forward") {
      subject = /^\s*fwd?:/i.test(subject) ? subject : "Fwd: " + subject;
    } else {
      subject = /^\s*re:/i.test(subject) ? subject : "Re: " + subject;
      var replyTarget = addrOf(m.replyTo) || addrOf(m.from);
      if (replyTarget) to.push(replyTarget);
      if (kind === "replyall") {
        (m.to || []).forEach(function (a) { if (a.a && to.indexOf(a.a) < 0) to.push(a.a); });
        (m.cc || []).forEach(function (a) { if (a.a && cc.indexOf(a.a) < 0 && to.indexOf(a.a) < 0) cc.push(a.a); });
      }
    }

    if (!to.length && kind !== "forward") {
      status("This message has no usable reply address.");
      return;
    }

    var params = [];
    if (subject) params.push("subject=" + encodeURIComponent(subject));
    if (cc.length) params.push("cc=" + encodeURIComponent(cc.join(",")));
    var body = quotedBody(m);
    if (kind === "forward") {
      body = "\n\n-------- Original Message --------\nSubject: " + (m.subject || "") +
             "\nDate: " + fmtDateLong(m.date) +
             "\nFrom: " + (m.from ? (m.from.n ? m.from.n + " <" + m.from.a + ">" : m.from.a) : "") +
             "\n\n" + (m.quote || "");
    }
    // Keep the URL inside what mail clients and browsers actually accept.
    if (body.length > 1400) body = body.slice(0, 1400) + "\n[… quoted text truncated …]";
    params.push("body=" + encodeURIComponent(body));
    if (kind !== "forward" && m.messageId) {
      params.push("in-reply-to=" + encodeURIComponent("<" + m.messageId + ">"));
    }

    var url = "mailto:" + encodeURIComponent(to.join(",")).replace(/%40/g, "@").replace(/%2C/g, ",") +
              "?" + params.join("&");
    status("Composing " + (kind === "forward" ? "forward" : "reply") + "…");
    window.location.href = url;
  }

  function updateToolbarState() {
    var has = !!currentMeta;
    ["btn-reply", "btn-replyall", "btn-forward", "btn-print", "btn-file"].forEach(function (id) {
      var b = document.getElementById(id);
      if (b) b.disabled = !has;
    });
  }

  // ================================================================= search

  function ensureSearchMeta() {
    if (app.searchMeta) return Promise.resolve(app.searchMeta);
    return getJSON("data/search/meta.json").then(function (m) { app.searchMeta = m; return m; });
  }

  function ensureDocs() {
    if (app.docs) return Promise.resolve(app.docs);
    return getJSON("data/search/docs.json").then(function (d) { app.docs = d; return d; });
  }

  function shardKey(token, len) {
    var key = token.slice(0, len).replace(/[^a-z0-9]/g, "_");
    return key || "_";
  }

  function loadShard(key) {
    if (app.shardCache[key]) return Promise.resolve(app.shardCache[key]);
    if (app.searchMeta.shards.indexOf(key) < 0) {
      app.shardCache[key] = {};
      return Promise.resolve(app.shardCache[key]);
    }
    return getJSON("data/search/" + key + ".json").then(function (t) {
      app.shardCache[key] = t;
      return t;
    });
  }

  function tokenizeQuery(q) {
    var folded = q.toLowerCase().normalize ? q.toLowerCase().normalize("NFKD").replace(/[̀-ͯ]/g, "") : q.toLowerCase();
    var out = [], m, re = /[a-z0-9][a-z0-9'._+\-]*/g;
    while ((m = re.exec(folded))) {
      var t = m[0].replace(/^['._+\-]+|['._+\-]+$/g, "");
      if (t.length > 1) out.push(t);
    }
    return out;
  }

  /* Postings are [gidDelta, fieldMask, ...]; expand to {gid: mask}. */
  function expand(flat, into) {
    var gid = 0;
    for (var i = 0; i < flat.length; i += 2) {
      gid += flat[i];
      into[gid] = (into[gid] || 0) | flat[i + 1];
    }
    return into;
  }

  function runSearch(query, scope) {
    var terms = tokenizeQuery(query);
    if (!terms.length) {
      renderResults([], "Type at least two characters.");
      return;
    }
    status("Searching…");
    progress(20);

    ensureSearchMeta()
      .then(function (meta) {
        return ensureDocs().then(function () { return meta; });
      })
      .then(function (meta) {
        var keys = {};
        terms.forEach(function (t) { keys[shardKey(t, meta.shardLen)] = 1; });
        return Promise.all(Object.keys(keys).map(loadShard)).then(function () { return meta; });
      })
      .then(function (meta) {
        progress(65);
        var acc = null;
        terms.forEach(function (term, ti) {
          var table = app.shardCache[shardKey(term, meta.shardLen)] || {};
          var hits = {};
          // Exact token, plus prefix expansion so "andree" finds "andreessen".
          if (table[term]) expand(table[term], hits);
          if (term.length >= meta.shardLen) {
            for (var tok in table) {
              if (tok !== term && tok.indexOf(term) === 0) expand(table[tok], hits);
            }
          }
          if (ti === 0) { acc = hits; return; }
          var next = {};
          for (var g in hits) if (acc[g] != null) next[g] = acc[g] | hits[g];
          acc = next;
        });

        var out = [];
        for (var gid in acc) {
          var loc = app.docs[gid];
          if (!loc) continue;
          out.push({ gid: +gid, mask: acc[gid], slug: loc[0], idx: loc[1] });
        }

        if (scope && scope !== "*") out = out.filter(function (h) { return h.slug === scope; });

        // Need the rows to rank and display; fetch just the folders involved.
        var slugs = {};
        out.forEach(function (h) { slugs[h.slug] = 1; });
        return Promise.all(Object.keys(slugs).map(function (s) {
          var f = app.byslug[s];
          return f ? loadFolderRows(f) : Promise.resolve([]);
        })).then(function () { return out; });
      })
      .then(function (hits) {
        hits.forEach(function (h) {
          var rows = app.listCache[h.slug] || [];
          h.row = rows[h.idx];
        });
        hits = hits.filter(function (h) { return !!h.row; });
        hits.forEach(function (h) {
          var s = 0;
          if (h.mask & F_SUBJECT) s += 60;
          if (h.mask & F_SENDER) s += 40;
          if (h.mask & F_RECIPIENT) s += 15;
          if (h.mask & F_ATTACH) s += 12;
          if (h.mask & F_BODY) s += 6;
          // Gentle recency tilt so a decade-old archive still surfaces the
          // relevant thread rather than the oldest one.
          s += Math.min(20, (h.row[R_DATE] || 0) / 86400 / 3650);
          h.score = s;
        });
        hits.sort(function (a, b) { return b.score - a.score || (b.row[R_DATE] || 0) - (a.row[R_DATE] || 0); });
        app.results = hits.slice(0, 500);
        app.resultSel = -1;
        progress(100);
        renderResults(app.results, hits.length > 500
          ? "Showing first 500 of " + hits.length + " matches."
          : hits.length + " match" + (hits.length === 1 ? "" : "es") + ".");
        status(hits.length + " message" + (hits.length === 1 ? "" : "s") + " matched “" + query + "”");
      })
      .catch(function (err) {
        progress(100);
        renderResults([], "Search failed: " + err.message);
      });
  }

  function highlight(text, terms) {
    var out = esc(text || "");
    terms.forEach(function (t) {
      if (t.length < 2) return;
      var re = new RegExp("(" + t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + ")", "ig");
      out = out.replace(re, "<mark>$1</mark>");
    });
    return out;
  }

  function renderResults(hits, note) {
    var host = $("#ps-results");
    host.textContent = "";
    $("#ps-search-note").textContent = note || "";
    var terms = tokenizeQuery(app.lastQuery);
    hits.forEach(function (h, i) {
      var r = h.row;
      var node = el("div", "ps-result");
      node.dataset.index = i;
      var subj = el("div", "r-subject");
      subj.innerHTML = highlight(r[R_SUBJ] || "(no subject)", terms);
      node.appendChild(subj);
      var send = el("div", "r-sender");
      send.innerHTML = highlight(r[R_FNAME] || r[R_FADDR] || "", terms);
      node.appendChild(send);
      node.appendChild(el("div", "r-folder", (app.byslug[h.slug] || {}).path || h.slug));
      node.appendChild(el("div", "r-date", fmtDate(r[R_DATE])));
      node.addEventListener("click", function () { selectResult(i); });
      node.addEventListener("dblclick", function () { goToResult(i); });
      host.appendChild(node);
    });
  }

  function selectResult(i) {
    app.resultSel = i;
    $$("#ps-results .ps-result").forEach(function (n, j) {
      n.classList.toggle("selected", j === i);
    });
  }

  function goToResult(i) {
    var h = app.results[i];
    if (!h) return;
    closeDialog();
    var uid = h.row[R_UID];
    if (!app.folder || app.folder.slug !== h.slug) {
      var f = app.byslug[h.slug];
      if (f) {
        app.folder = f;
        renderTree();
        $("#ps-folder-title").textContent = f.path;
        app.rows = app.listCache[h.slug] || [];
        applyView();
      }
    }
    var idx = app.flatRows.findIndex(function (e) { return e.row[R_UID] === uid; });
    if (idx >= 0) { app.selected = idx; renderList(); scrollSelectedIntoView(); }
    openMessage(uid);
  }

  function openDialog() {
    $("#ps-search-back").classList.add("open");
    var input = $("#ps-search-input");
    input.focus();
    input.select();
    var scope = $("#ps-search-scope");
    if (scope.options.length <= 1) {
      app.folders.forEach(function (f) {
        var o = document.createElement("option");
        o.value = f.slug;
        o.textContent = f.path;
        scope.appendChild(o);
      });
    }
  }
  function closeDialog() { $("#ps-search-back").classList.remove("open"); }

  // ============================================================ misc actions

  function openStandalone(uid) { window.open(pageFor(uid), "_blank", "noopener"); }

  function nextMessage(unreadOnly) {
    var start = app.selected + 1;
    for (var i = start; i < app.flatRows.length; i++) {
      if (!unreadOnly || isUnread(app.flatRows[i].row)) { selectRow(i); scrollSelectedIntoView(); return; }
    }
    status(unreadOnly ? "No more unread messages in this folder." : "End of folder.");
  }

  function moveSelection(delta) {
    var next = app.selected < 0 ? 0 : app.selected + delta;
    next = Math.max(0, Math.min(app.flatRows.length - 1, next));
    if (next !== app.selected || app.selected < 0) { selectRow(next); scrollSelectedIntoView(); }
  }

  // ================================================================== splitters

  function initSplitter(handle, target, axis, invert) {
    handle.addEventListener("mousedown", function (down) {
      down.preventDefault();
      var startPos = axis === "x" ? down.clientX : down.clientY;
      var startSize = axis === "x" ? target.offsetWidth : target.offsetHeight;
      document.body.style.cursor = axis === "x" ? "col-resize" : "row-resize";

      function move(ev) {
        var delta = (axis === "x" ? ev.clientX : ev.clientY) - startPos;
        var size = startSize + (invert ? -delta : delta);
        size = Math.max(60, size);
        if (axis === "x") target.style.width = size + "px";
        else { target.style.height = size + "px"; target.style.flex = "0 0 " + size + "px"; }
        renderList();
      }
      function up() {
        document.removeEventListener("mousemove", move);
        document.removeEventListener("mouseup", up);
        document.body.style.cursor = "";
      }
      document.addEventListener("mousemove", move);
      document.addEventListener("mouseup", up);
    });
  }

  function initColumnResize() {
    $$("#ps-list-head .grip").forEach(function (grip) {
      grip.addEventListener("mousedown", function (down) {
        down.preventDefault();
        down.stopPropagation();
        var col = grip.parentNode;
        var startX = down.clientX;
        var startW = col.offsetWidth;
        function move(ev) {
          var w = Math.max(40, startW + ev.clientX - startX);
          col.style.flexBasis = w + "px";
          col.style.flexGrow = "0";
          renderList();
        }
        function up() {
          document.removeEventListener("mousemove", move);
          document.removeEventListener("mouseup", up);
        }
        document.addEventListener("mousemove", move);
        document.addEventListener("mouseup", up);
      });
    });
  }

  // ===================================================================== menus

  function initMenus() {
    var menus = $$(".ps-menu");
    menus.forEach(function (m) {
      m.addEventListener("click", function (ev) {
        if (ev.target.closest(".ps-menu-item")) return;
        var wasOpen = m.classList.contains("open");
        menus.forEach(function (o) { o.classList.remove("open"); });
        if (!wasOpen) m.classList.add("open");
        ev.stopPropagation();
      });
      m.addEventListener("mouseenter", function () {
        if (menus.some(function (o) { return o.classList.contains("open"); })) {
          menus.forEach(function (o) { o.classList.remove("open"); });
          m.classList.add("open");
        }
      });
    });
    document.addEventListener("click", function () {
      menus.forEach(function (o) { o.classList.remove("open"); });
    });

    $$(".ps-menu-item[data-action]").forEach(function (item) {
      item.addEventListener("click", function () {
        menus.forEach(function (o) { o.classList.remove("open"); });
        // A greyed item closes the menu and does nothing else. Without this it
        // still ran its action, which only went unnoticed because every
        // disabled item happened to be wired to "noop".
        if (item.classList.contains("disabled")) return;
        doAction(item.dataset.action, item);
      });
    });
  }

  function doAction(action, node) {
    switch (action) {
      case "reply": mailto("reply"); break;
      case "replyall": mailto("replyall"); break;
      case "forward": mailto("forward"); break;
      case "search": openDialog(); break;
      case "print": window.print(); break;
      case "next": nextMessage(false); break;
      case "nextunread": nextMessage(true); break;
      case "open": if (currentMeta) openStandalone(currentMeta.uid); break;
      case "threaded":
        app.threaded = !app.threaded;
        if (node) node.classList.toggle("checked", app.threaded);
        applyView();
        status(app.threaded ? "Threaded view" : "Flat view");
        break;
      case "unreadonly":
        app.unreadOnly = !app.unreadOnly;
        if (node) node.classList.toggle("checked", app.unreadOnly);
        applyView();
        break;
      case "markread":
        (app.listCache[app.folder && app.folder.slug] || []).forEach(function (r) { app.read[r[R_UID]] = 1; });
        saveRead();
        if (app.folder) { app.folder.unread = 0; renderTree(); }
        applyView();
        status("Folder marked read.");
        break;
      case "markunread":
        app.read = {};
        saveRead();
        app.folders.forEach(function (f) { f.unread = f.unreadOriginal; });
        renderTree();
        applyView();
        status("Read marks cleared.");
        break;
      case "about": $("#ps-about-back").classList.add("open"); break;
      case "sortdate": app.sortCol = "date"; applyView(); break;
      case "sortsubject": app.sortCol = "subject"; applyView(); break;
      case "sortsender": app.sortCol = "sender"; applyView(); break;
      case "file":
      case "delete":
        status("The archive is read-only.");
        break;
      case "noop": break;
      default: break;
    }
  }

  // ==================================================================== boot

  function initToolbar() {
    $$(".ps-tbtn[data-action]").forEach(function (b) {
      b.addEventListener("click", function () { doAction(b.dataset.action, b); });
    });
  }

  function initKeys() {
    document.addEventListener("keydown", function (ev) {
      var inField = /^(INPUT|SELECT|TEXTAREA)$/.test(ev.target.tagName);
      if (ev.key === "Escape") {
        closeDialog();
        $("#ps-about-back").classList.remove("open");
        return;
      }
      if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "f") {
        ev.preventDefault(); openDialog(); return;
      }
      if (inField) {
        if (ev.key === "Enter" && ev.target.id === "ps-search-input") {
          ev.preventDefault();
          app.lastQuery = ev.target.value;
          runSearch(app.lastQuery, $("#ps-search-scope").value);
        }
        if (ev.target.id === "ps-search-input" && (ev.key === "ArrowDown" || ev.key === "ArrowUp")) {
          ev.preventDefault();
          var n = app.resultSel + (ev.key === "ArrowDown" ? 1 : -1);
          if (n >= 0 && n < app.results.length) selectResult(n);
        }
        return;
      }
      switch (ev.key) {
        case "ArrowDown": ev.preventDefault(); moveSelection(1); break;
        case "ArrowUp": ev.preventDefault(); moveSelection(-1); break;
        case "PageDown": ev.preventDefault(); moveSelection(15); break;
        case "PageUp": ev.preventDefault(); moveSelection(-15); break;
        case "Home": ev.preventDefault(); if (app.flatRows.length) { selectRow(0); scrollSelectedIntoView(); } break;
        case "End": ev.preventDefault(); if (app.flatRows.length) { selectRow(app.flatRows.length - 1); scrollSelectedIntoView(); } break;
        case "Enter": if (currentMeta) openStandalone(currentMeta.uid); break;
        case "n": case "N": nextMessage(true); break;
        case "r": case "R": mailto(ev.shiftKey ? "replyall" : "reply"); break;
      }
    });
  }

  function initSortHeaders() {
    $$("#ps-list-head .col[data-col]").forEach(function (c) {
      c.addEventListener("click", function (ev) {
        if (ev.target.classList.contains("grip")) return;
        var col = c.dataset.col;
        if (app.sortCol === col) app.sortDir = -app.sortDir;
        else { app.sortCol = col; app.sortDir = col === "date" ? -1 : 1; }
        applyView();
      });
    });
  }

  function routeFromHash() {
    var hash = location.hash.replace(/^#/, "");
    if (!hash) return false;
    var m = /^msg=([0-9a-f]+)$/.exec(hash);
    if (m) {
      revealMessage(m[1]);
      return true;
    }
    m = /^folder=([\w\-.]+)$/.exec(hash);
    if (m && app.byslug[m[1]]) { selectFolder(m[1], true); return true; }
    m = /^q=(.*)$/.exec(hash);
    if (m) {
      app.lastQuery = decodeURIComponent(m[1]);
      openDialog();
      $("#ps-search-input").value = app.lastQuery;
      runSearch(app.lastQuery, "*");
      return false;
    }
    return false;
  }

  function boot() {
    loadRead();
    status("Opening Pastscape…");
    progress(15);
    getJSON("data/folders.json").then(function (cfg) {
      app.cfg = cfg;
      app.folders = cfg.folders || [];
      app.folders.forEach(function (f) {
        f.unreadOriginal = f.unread;
        // Reflect locally-stored read marks in the initial counts.
        app.byslug[f.slug] = f;
      });
      $("#ps-title-text").textContent = (cfg.title || "Pastscape") + " - Pastscape Messenger";
      $("#ps-built").textContent = "Built " + (cfg.built || "").slice(0, 10);
      $("#ps-total").textContent = cfg.totalMessages + " messages";
      renderTree();
      progress(60);

      initMenus();
      initToolbar();
      initSortHeaders();
      initColumnResize();
      initKeys();
      initSplitter($("#ps-split-v"), $("#ps-pane-tree"), "x", false);
      initSplitter($("#ps-split-h"), $("#ps-pane-list"), "y", false);

      $("#ps-list-scroll").addEventListener("scroll", renderList, { passive: true });
      window.addEventListener("resize", renderList);

      $("#ps-search-go").addEventListener("click", function () {
        app.lastQuery = $("#ps-search-input").value;
        runSearch(app.lastQuery, $("#ps-search-scope").value);
      });
      $("#ps-search-close").addEventListener("click", closeDialog);
      $("#ps-search-open").addEventListener("click", function () {
        if (app.resultSel >= 0) goToResult(app.resultSel);
      });
      $$("#ps-about-back .ps-close, #ps-about-ok").forEach(function (b) {
        b.addEventListener("click", function () { $("#ps-about-back").classList.remove("open"); });
      });
      $("#ps-search-back .ps-close").addEventListener("click", closeDialog);

      // Ignore the hashchange our own navigation just caused, otherwise every
      // click on a row would re-fetch the message it already displayed.
      window.addEventListener("hashchange", function () {
        if (location.hash.replace(/^#/, "") === app.selfHash) return;
        routeFromHash();
      });

      progress(100);
      if (!routeFromHash()) {
        var first = app.folders.find(function (f) { return f.count > 0; }) || app.folders[0];
        if (first) selectFolder(first.slug, true);
        else { status("Archive is empty."); showNoMessage(); }
      }
    }).catch(function (err) {
      status("Could not load archive: " + err.message);
      progress(100);
      var pane = $("#ps-msgpane");
      pane.textContent = "";
      pane.appendChild(el("div", "ps-nomsg",
        "Could not load data/folders.json (" + err.message + "). " +
        "If you opened this file directly, serve the folder over HTTP instead: python3 -m http.server"));
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
