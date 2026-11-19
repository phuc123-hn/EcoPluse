/* EcoPulse – chuyển ngôn ngữ EN / VI (không dùng Google Dịch)
 *
 * Cách hoạt động
 *  - Mã nguồn các trang vẫn viết tiếng Việt. Khi chọn English, file này tra từ điển (i18n-en.js) rồi thay chữ
 *    ngay khi chữ xuất hiện trên trang (kể cả chữ do JavaScript vẽ ra sau này). Chọn VI thì trả lại nguyên văn.
 *  - Chỉ đổi những đoạn chữ CÓ trong từ điển, nên nhẹ hơn Google Dịch rất nhiều và không bị "nửa Việt nửa Anh".
 *  - Lựa chọn được nhớ trong localStorage ("ep_lang"), dùng chung cho trang chính và các khung bản đồ (iframe).
 *  - Mặc định là English.
 *  - Nội dung người dùng tự viết (tin nhắn, bài viết) nằm trong phần tử có translate="no" hoặc class "notranslate" nên không bị đổi.
 *  - Thêm ?i18n=debug vào địa chỉ để xem trong Console những đoạn tiếng Việt chưa có trong từ điển.
 */
(function () {
  "use strict";
  var KEY = "ep_lang";
  var D = window.EP_EN || {};
  var RX = (window.EP_EN_RX || []).map(function (p) { return [new RegExp(p[0]), p[1]]; });
  var VI = /[àáạảãâấầẩẫậăắằẳẵặèéẹẻẽêếềểễệìíịỉĩòóọỏõôốồổỗộơớờởỡợùúụủũưứừửữựỳýỵỷỹđ]/i;
  var DEBUG = /[?&]i18n=debug\b/.test(location.search);
  var ATTRS = ["title", "placeholder", "aria-label", "alt", "data-ph"];
  var SKIP = { SCRIPT: 1, STYLE: 1, NOSCRIPT: 1, TEXTAREA: 1 };
  var own = Object.prototype.hasOwnProperty;
  var missing = {};

  var lang = "en";
  try { var s = localStorage.getItem(KEY); if (s === "vi" || s === "en") lang = s; } catch (e) {}

  /* ---------- tra từ điển ---------- */
  function lookup(t) {
    if (own.call(D, t)) return D[t];
    var n = t.replace(/\s+/g, " ");
    if (n !== t && own.call(D, n)) return D[n];
    for (var i = 0; i < RX.length; i++) if (RX[i][0].test(t)) return t.replace(RX[i][0], RX[i][1]);
    var m = t.match(/^([^\p{L}\p{N}]*)([\s\S]*?)([^\p{L}\p{N}]*)$/u);
    if (m && m[2] && m[2] !== t && own.call(D, m[2])) return m[1] + D[m[2]] + m[3];
    return null;
  }
  function tr(s) {
    var t = s.trim();
    if (!t) return s;
    var r = lookup(t);
    if (r == null) { if (DEBUG && VI.test(t)) missing[t] = 1; return s; }
    var i = s.indexOf(t);
    return s.slice(0, i) + r + s.slice(i + t.length);
  }
  window.epT = function (s) { return lang === "en" && typeof s === "string" ? tr(s) : s; };

  /* ---------- đổi chữ trong DOM ---------- */
  var textRec = new WeakMap(), attrRec = new WeakMap();

  function skipped(el) {
    return !el || (el.nodeType === 1 && (SKIP[el.tagName] || (el.closest && el.closest('[translate="no"],.notranslate'))));
  }
  function doText(node) {
    var rec = textRec.get(node), cur = node.data;
    if (rec && cur === rec.en) return;
    var p = node.parentElement;
    if (skipped(p)) return;
    var en = tr(cur);
    if (en !== cur) { textRec.set(node, { vi: cur, en: en }); node.data = en; }
    else if (rec) textRec.delete(node);
  }
  function doAttrs(el) {
    if (skipped(el)) return;
    var rec = attrRec.get(el);
    for (var i = 0; i < ATTRS.length; i++) {
      var a = ATTRS[i], v = el.getAttribute(a);
      if (v == null) continue;
      if (rec && rec[a] && rec[a].en === v) continue;
      var en = tr(v);
      if (en !== v) {
        if (!rec) attrRec.set(el, rec = {});
        rec[a] = { vi: v, en: en };
        el.setAttribute(a, en);
      } else if (rec && rec[a]) delete rec[a];
    }
    if (el.tagName === "INPUT" && /^(button|submit)$/.test(el.type) && el.value) {
      var nv = tr(el.value); if (nv !== el.value) el.value = nv;
    }
  }
  function sweep(root, toEn) {
    if (!root) return;
    if (root.nodeType === 3) { toEn ? doText(root) : undoText(root); return; }
    if (root.nodeType !== 1 && root.nodeType !== 9 && root.nodeType !== 11) return;
    if (root.nodeType === 1) { toEn ? doAttrs(root) : undoAttrs(root); if (SKIP[root.tagName]) return; }
    var w = document.createTreeWalker(root, 5 /* SHOW_ELEMENT | SHOW_TEXT */, null), n;
    while ((n = w.nextNode())) {
      if (n.nodeType === 3) toEn ? doText(n) : undoText(n);
      else { toEn ? doAttrs(n) : undoAttrs(n); }
    }
  }
  function undoText(node) {
    var rec = textRec.get(node);
    if (rec && node.data === rec.en) node.data = rec.vi;
    textRec.delete(node);
  }
  function undoAttrs(el) {
    var rec = attrRec.get(el);
    if (!rec) return;
    for (var a in rec) if (el.getAttribute(a) === rec[a].en) el.setAttribute(a, rec[a].vi);
    attrRec.delete(el);
  }

  /* ---------- theo dõi nội dung vẽ thêm (chỉ chạy khi đang ở English) ---------- */
  var obs = null;
  function watch() {
    if (obs || !window.MutationObserver) return;
    obs = new MutationObserver(function (list) {
      obs.disconnect();                       // không tự bắt lại những thay đổi do chính mình gây ra
      try {
        for (var i = 0; i < list.length; i++) {
          var m = list[i];
          if (m.type === "childList") { for (var j = 0; j < m.addedNodes.length; j++) sweep(m.addedNodes[j], true); }
          else if (m.type === "characterData") doText(m.target);
          else if (m.type === "attributes") doAttrs(m.target);
        }
      } finally { connect(); }
    });
    connect();
  }
  function connect() {
    if (obs) obs.observe(document.documentElement, { subtree: true, childList: true, characterData: true, attributes: true, attributeFilter: ATTRS });
  }
  function unwatch() { if (obs) { obs.disconnect(); obs = null; } }

  /* ---------- dialog alert/confirm/prompt ---------- */
  ["alert", "confirm", "prompt"].forEach(function (f) {
    var o = window[f]; if (typeof o !== "function") return;
    window[f] = function (msg) {
      var a = Array.prototype.slice.call(arguments);
      if (lang === "en" && typeof msg === "string") a[0] = msg.split("\n").map(function (l) { return tr(l); }).join("\n");
      return o.apply(window, a);
    };
  });

  /* ---------- nút VI / EN ---------- */
  var GLOBE = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c2.6 2.7 3.9 5.7 3.9 9s-1.3 6.3-3.9 9c-2.6-2.7-3.9-5.7-3.9-9S9.4 5.7 12 3Z"/></svg>';
  var btn = null;
  function paintBtn() {
    if (!btn) return;
    var next = lang === "en" ? "VI" : "EN";    // nút ghi ngôn ngữ sẽ chuyển sang (giống bản trước: đang English thì nút ghi VI)
    var t = btn.querySelector(".lang-txt");
    if (t) t.textContent = next;
    var tip = lang === "en" ? "Chuyển sang tiếng Việt" : "Switch to English";
    btn.title = tip; btn.setAttribute("aria-label", tip);
  }
  function mountBtn() {
    btn = document.getElementById("lang");
    if (!btn && window.top === window) {          // trang chạy riêng (/map, /dat): thêm nút nổi; trong iframe thì dùng nút của trang chính
      btn = document.createElement("button");
      btn.id = "lang"; btn.type = "button"; btn.className = "lang-toggle ep-float";
      btn.innerHTML = GLOBE + '<span class="lang-txt"></span>';
      var st = document.createElement("style");
      st.textContent = ".ep-float{position:fixed;top:10px;right:54px;z-index:99999;display:flex;align-items:center;gap:5px;height:34px;padding:0 10px;border:1px solid rgba(47,158,90,.45);border-radius:12px;background:#fff;color:#1d6b3c;font:800 12px system-ui,sans-serif;letter-spacing:.04em;cursor:pointer;box-shadow:0 2px 8px rgba(0,0,0,.18)}.ep-float svg{width:16px;height:16px}@media(prefers-color-scheme:dark){.ep-float{background:#1d2a23;color:#8fd6ab}}";
      document.head.appendChild(st); document.body.appendChild(btn);
    }
    if (!btn) return;
    if (!btn.querySelector(".lang-txt")) btn.innerHTML = GLOBE + '<span class="lang-txt"></span>';
    btn.onclick = function () { setLang(lang === "en" ? "vi" : "en", true); };
    paintBtn();
  }

  /* ---------- đổi ngôn ngữ ---------- */
  function setLang(l, save) {
    if (l !== "en" && l !== "vi") return;
    var changed = l !== lang;
    lang = l;
    if (save) { try { localStorage.setItem(KEY, l); } catch (e) {} }
    document.documentElement.lang = l;
    if (l === "en") { sweep(document.documentElement, true); watch(); }
    else { unwatch(); sweep(document.documentElement, false); }
    paintBtn();
    if (changed) { try { window.dispatchEvent(new CustomEvent("ep-lang", { detail: l })); } catch (e) {} }
    if (DEBUG) setTimeout(report, 800);
  }
  function report() {
    var k = Object.keys(missing);
    if (k.length) console.info("[i18n] " + k.length + " đoạn tiếng Việt chưa có trong từ điển:\n" + k.join("\n"));
  }
  window.epSetLang = function (l) { setLang(l, true); };
  window.epLang = function () { return lang; };

  /* trang chính đổi thì các khung bản đồ (iframe) và tab khác đổi theo */
  window.addEventListener("storage", function (e) { if (e.key === KEY && (e.newValue === "en" || e.newValue === "vi")) setLang(e.newValue, false); });

  /* ---------- khởi động ---------- */
  document.documentElement.lang = lang;
  if (lang === "en") watch();                    // bắt chữ ngay từ lúc trang đang được dựng, trước khi trình duyệt vẽ
  function ready() {
    mountBtn();
    if (lang === "en") sweep(document.documentElement, true);
    if (DEBUG) setTimeout(report, 1500);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", ready); else ready();
})();
