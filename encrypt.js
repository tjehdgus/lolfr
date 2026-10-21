// page.html 을 아이디+비밀번호로 암호화해 index.html(로그인 화면) 로 만든다.
// 사용법: node encrypt.js <아이디> <비밀번호>
// 키 = PBKDF2-SHA256(아이디 + "\n" + 비밀번호, 랜덤 salt, 60만 회) → AES-256-GCM
// 아이디·비밀번호는 어디에도 저장하지 않는다. 둘 다 맞아야 복호화된다.
const crypto = require("crypto");
const fs = require("fs");
const path = require("path");

const [, , id, pw] = process.argv;
if (!id || !pw) {
  console.error("사용법: node encrypt.js <아이디> <비밀번호>");
  process.exit(1);
}

const ITERATIONS = 600000;
// page.html 은 문서 틀이 없는 조각이라 앞에 doctype 을 붙여 표준 모드로 열리게 함
const src = '<!doctype html><html lang="ko"><meta charset="utf-8">' +
  '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">' +
  '<meta name="robots" content="noindex">' +
  fs.readFileSync(path.join(__dirname, "page.html"), "utf8");
const salt = crypto.randomBytes(16);
const iv = crypto.randomBytes(12);
const key = crypto.pbkdf2Sync(`${id}\n${pw}`, salt, ITERATIONS, 32, "sha256");
const cipher = crypto.createCipheriv("aes-256-gcm", key, iv);
// WebCrypto 는 암호문 뒤에 인증 태그가 붙은 형태를 기대함
const sealed = Buffer.concat([cipher.update(src, "utf8"), cipher.final(), cipher.getAuthTag()]);

const payload = JSON.stringify({
  salt: salt.toString("base64"), iv: iv.toString("base64"),
  data: sealed.toString("base64"), iterations: ITERATIONS,
});

const html = `<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="robots" content="noindex">
<title>올빼미 내전</title>
<style>
:root { --bg: #f3f4f8; --surface: #fff; --ink: #141a2b; --muted: #5d6680; --line: #d9dde8; --gold: #8c6a24; --bad: #b8452f; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #0b1020; --surface: #131a2e; --ink: #e8e3d4; --muted: #8e97ae; --line: #263052; --gold: #c8aa6e; --bad: #ff8a6e; color-scheme: dark; }
}
* { box-sizing: border-box; }
html, body { height: 100%; }
body { margin: 0; background: var(--bg); color: var(--ink); display: grid; place-items: center;
  font-family: "IBM Plex Sans KR", "Apple SD Gothic Neo", "Malgun Gothic", system-ui, sans-serif;
  padding: 24px max(16px, env(safe-area-inset-right, 0px)) 24px max(16px, env(safe-area-inset-left, 0px)); }
form { width: 100%; max-width: 340px; display: grid; gap: 14px; background: var(--surface);
  border: 1px solid var(--line); border-radius: 12px; padding: 28px 24px; }
.eyebrow { font-size: 12px; letter-spacing: .12em; color: var(--gold); font-weight: 600; }
h1 { margin: 0 0 6px; font-size: 26px; font-weight: 700; }
label { display: grid; gap: 6px; font-size: 13px; color: var(--muted); }
input[type=text], input[type=password] { font: inherit; font-size: 16px; color: var(--ink); background: var(--bg);
  border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px; }
input:focus-visible, button:focus-visible { outline: 2px solid var(--gold); outline-offset: 2px; }
.remember { display: flex; gap: 8px; align-items: center; font-size: 13px; color: var(--muted); }
button { font: inherit; font-weight: 600; font-size: 15px; border: 0; border-radius: 8px; padding: 11px;
  background: var(--gold); color: var(--bg); cursor: pointer; }
button:disabled { opacity: .5; cursor: wait; }
.error { min-height: 1.2em; margin: 0; font-size: 13px; color: var(--bad); }
</style>
</head>
<body>
<form id="login" autocomplete="on">
  <div>
    <div class="eyebrow">칼바람 증강 · 사용자 설정 내전</div>
    <h1>올빼미 내전</h1>
  </div>
  <label>아이디<input type="text" id="uid" name="username" autocomplete="username" required></label>
  <label>비밀번호<input type="password" id="pw" name="password" autocomplete="current-password" required></label>
  <label class="remember"><input type="checkbox" id="remember"> 이 기기에서 로그인 유지</label>
  <button id="go">들어가기</button>
  <p class="error" id="err" role="alert"></p>
</form>
<script>
const P = ${payload};
const STORE = "owl-naejeon-key:" + P.salt;
const b64 = (s) => Uint8Array.from(atob(s), (c) => c.charCodeAt(0));

async function open(rawKey) {
  const key = await crypto.subtle.importKey("raw", rawKey, "AES-GCM", false, ["decrypt"]);
  const plain = await crypto.subtle.decrypt({ name: "AES-GCM", iv: b64(P.iv) }, key, b64(P.data));
  document.open();
  document.write(new TextDecoder().decode(plain));
  document.close();
}

async function derive(id, pw) {
  const base = await crypto.subtle.importKey("raw", new TextEncoder().encode(id + "\\n" + pw), "PBKDF2", false, ["deriveBits"]);
  const bits = await crypto.subtle.deriveBits({ name: "PBKDF2", hash: "SHA-256", salt: b64(P.salt), iterations: P.iterations }, base, 256);
  return new Uint8Array(bits);
}

(async () => {
  let saved = null;
  try { saved = localStorage.getItem(STORE); } catch (e) {}
  if (saved) {
    try { await open(b64(saved)); return; } catch (e) { try { localStorage.removeItem(STORE); } catch (e2) {} }
  }
})();

document.getElementById("login").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = document.getElementById("go"), err = document.getElementById("err");
  btn.disabled = true; err.textContent = "확인 중...";
  try {
    const remember = document.getElementById("remember").checked; // open() 후엔 이 폼이 사라짐
    const raw = await derive(document.getElementById("uid").value.trim(), document.getElementById("pw").value);
    await open(raw);
    if (remember) {
      try { localStorage.setItem(STORE, btoa(String.fromCharCode(...raw))); } catch (e2) {}
    }
  } catch (ex) {
    err.textContent = "아이디 또는 비밀번호가 맞지 않아요.";
    btn.disabled = false;
  }
});
</script>
</body>
</html>
`;

fs.writeFileSync(path.join(__dirname, "index.html"), html);
console.log("index.html 생성 완료");
