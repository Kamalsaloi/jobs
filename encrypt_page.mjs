// Encrypts a report into a self-contained page that asks for a password and decrypts in the browser.
// Usage: PAGE_PASSWORD=... node encrypt_page.mjs <in.html> <out.html> <salt-file>
// AES-256-GCM with a PBKDF2-SHA256 key. The salt is fixed per install so "remember this device" survives daily updates.
import { readFileSync, writeFileSync, existsSync, mkdirSync } from "node:fs";
import { dirname } from "node:path";
import { webcrypto as crypto } from "node:crypto";

const [, , src, out, saltFile] = process.argv;
const password = process.env.PAGE_PASSWORD;
if (!src || !out || !saltFile || !password) {
  console.error("usage: PAGE_PASSWORD=... node encrypt_page.mjs <in.html> <out.html> <salt-file>");
  process.exit(2);
}
const ITERATIONS = 310000;
const b64 = (u8) => Buffer.from(u8).toString("base64");

if (!existsSync(saltFile)) writeFileSync(saltFile, b64(crypto.getRandomValues(new Uint8Array(16))));
const salt = Buffer.from(readFileSync(saltFile, "utf8").trim(), "base64");
const iv = crypto.getRandomValues(new Uint8Array(12));

const base = await crypto.subtle.importKey("raw", new TextEncoder().encode(password), "PBKDF2", false, ["deriveBits"]);
const bits = await crypto.subtle.deriveBits({ name: "PBKDF2", salt, iterations: ITERATIONS, hash: "SHA-256" }, base, 256);
const key = await crypto.subtle.importKey("raw", bits, "AES-GCM", false, ["encrypt"]);
const ct = new Uint8Array(await crypto.subtle.encrypt({ name: "AES-GCM", iv }, key, readFileSync(src)));

const payload = JSON.stringify({ salt: b64(salt), iv: b64(iv), iter: ITERATIONS, ct: b64(ct) });

const page = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Job Matches</title>
<style>
  :root { --bg:#fafafa; --card:#fff; --fg:#0a0a0a; --muted:#737373; --border:#e5e5e5; --primary:#171717; --on-primary:#fafafa; --danger:#dc2626; }
  @media (prefers-color-scheme: dark) { :root { --bg:#0a0a0a; --card:#171717; --fg:#fafafa; --muted:#a3a3a3; --border:#262626; --primary:#e5e5e5; --on-primary:#171717; --danger:#f87171; } }
  * { box-sizing: border-box; }
  body { margin:0; min-height:100vh; display:grid; place-items:center; padding:16px; background:var(--bg); color:var(--fg);
         font:14px/1.5 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif; }
  form { width:100%; max-width:360px; background:var(--card); border:1px solid var(--border); border-radius:12px; padding:24px;
         display:grid; gap:14px; box-shadow:0 1px 2px rgb(0 0 0 / .05); }
  h1 { margin:0; font-size:18px; font-weight:600; } p { margin:0; color:var(--muted); }
  input[type=password] { width:100%; height:38px; padding:0 12px; border:1px solid var(--border); border-radius:8px;
         background:transparent; color:var(--fg); font:inherit; }
  input[type=password]:focus { outline:2px solid color-mix(in oklab, var(--fg) 25%, transparent); outline-offset:1px; }
  label { display:flex; gap:8px; align-items:center; color:var(--muted); cursor:pointer; }
  button { height:38px; border:0; border-radius:8px; background:var(--primary); color:var(--on-primary); font:inherit; font-weight:500; cursor:pointer; }
  button:disabled { opacity:.6; cursor:wait; }
  .err { color:var(--danger); min-height:1.5em; }
</style>
</head>
<body>
<form id="f" autocomplete="off">
  <div><h1>Job matches</h1><p>Enter the password to open today's report.</p></div>
  <input type="password" id="pw" placeholder="Password" aria-label="Password" autofocus required>
  <label><input type="checkbox" id="remember" checked> Remember on this device</label>
  <button id="go" type="submit">Unlock</button>
  <div class="err" id="err" role="alert"></div>
</form>
<script id="payload" type="application/json">${payload}</script>
<script>
(function () {
  var P = JSON.parse(document.getElementById("payload").textContent);
  var KEY = "jh.pagekey";
  function bytes(b64) { return Uint8Array.from(atob(b64), function (c) { return c.charCodeAt(0); }); }
  function b64(u8) { var s = ""; u8.forEach(function (b) { s += String.fromCharCode(b); }); return btoa(s); }
  function decrypt(rawKey) {
    return crypto.subtle.importKey("raw", rawKey, "AES-GCM", false, ["decrypt"])
      .then(function (k) { return crypto.subtle.decrypt({ name: "AES-GCM", iv: bytes(P.iv) }, k, bytes(P.ct)); })
      .then(function (pt) { return new TextDecoder().decode(pt); });
  }
  function show(html) { document.open(); document.write(html); document.close(); }
  function derive(pw) {
    return crypto.subtle.importKey("raw", new TextEncoder().encode(pw), "PBKDF2", false, ["deriveBits"])
      .then(function (base) { return crypto.subtle.deriveBits({ name: "PBKDF2", salt: bytes(P.salt), iterations: P.iter, hash: "SHA-256" }, base, 256); })
      .then(function (bits) { return new Uint8Array(bits); });
  }
  var saved = null;
  try { saved = JSON.parse(localStorage.getItem(KEY) || "null"); } catch (e) {}
  if (saved && saved.salt === P.salt) {
    decrypt(bytes(saved.key)).then(show).catch(function () { try { localStorage.removeItem(KEY); } catch (e) {} });
  }
  document.getElementById("f").addEventListener("submit", function (e) {
    e.preventDefault();
    var btn = document.getElementById("go"), err = document.getElementById("err");
    btn.disabled = true; btn.textContent = "Unlocking…"; err.textContent = "";
    derive(document.getElementById("pw").value).then(function (raw) {
      var remember = document.getElementById("remember").checked;
      return decrypt(raw).then(function (html) {
        try { if (remember) localStorage.setItem(KEY, JSON.stringify({ salt: P.salt, key: b64(raw) })); } catch (e) {}
        show(html);
      });
    }).catch(function () {
      btn.disabled = false; btn.textContent = "Unlock"; err.textContent = "Wrong password.";
    });
  });
})();
</script>
</body>
</html>
`;
mkdirSync(dirname(out), { recursive: true });
writeFileSync(out, page);
console.log(`encrypted ${readFileSync(src).length} bytes -> ${out}`);
