"""
ctyf_auth.py
────────────
Auto-login to ctyf.co.in and extract API payload keys for api-locate.com.

- Solves CAPTCHA automatically using ddddocr (no Tesseract needed)
- Saves session to ctyf_session.json and reuses it until expiry
- On session expiry, re-logs in automatically

Usage:
    from ctyf_auth import CtyFAuth
    auth = CtyFAuth()
    payload = auth.get_payload()   # returns dict of keys for api-locate.com
"""
import json
import time
import warnings
from pathlib import Path

import requests
from bs4 import BeautifulSoup

warnings.filterwarnings("ignore")

# ── Fixed mapping: cookie sub-key → api-locate.com payload field ─────────────
# These cookie sub-keys are FIXED across all sessions (server-generated, user-specific).
# Values inside them change per session and are what we send to api-locate.com.
COOKIE_KEY_TO_PAYLOAD = {
    "QyNr+/2vRtwHhSjehyGYWA":                        "kangve",
    "k38SnG1u0onD3Jq2d5zTmg":                        "wwacgg",
    "es6GHugJmI+mh1S6vWxSBA":                        "mzxkwl",   # also gisrls
    "tL4pYVn2Rpr2g9paxslp6w":                        "yztrxb",
    "DawoNjI+MqL3Ln2LZFYWQw":                        "megkef",
    "w1g7r4q8KteCc7+MPAFTO50Ch8yFajcU7+QwXjLSnm4":  "luuoza",
}

AUTH_COOKIE_NAME = "Q5oF3Ys2pFavhJETMA2Y1g"


class CtyFAuth:
    LOGIN_URL     = "https://ctyf.co.in/SiteLogin.aspx"
    CAPTCHA_URL   = "https://ctyf.co.in/api/captcha.aspx"
    DASHBOARD_URL = "https://ctyf.co.in/locateNxtGenDashboard.aspx"
    SESSION_FILE  = Path("ctyf_session.json")
    CONFIG_FILE   = Path("ctyf_config.json")

    def __init__(self):
        self.session = requests.Session()
        self.session.verify = False
        self.session.headers.update({
            "user-agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "accept-language": "en-US,en;q=0.9",
        })
        self._payload: dict = {}
        self._ocr = None  # lazy-load ddddocr

    # ── Public API ────────────────────────────────────────────────────────────

    def get_payload(self) -> dict:
        """
        Return payload keys for api-locate.com.
        Automatically handles login and session refresh.
        """
        if self._try_saved_session():
            return self._payload.copy()

        print("[Auth] Session missing or expired — logging in...")
        self._login()
        return self._payload.copy()

    # ── Session management ────────────────────────────────────────────────────

    def _try_saved_session(self) -> bool:
        """Load and verify saved session. Returns True if still valid."""
        if not self.SESSION_FILE.exists():
            return False
        try:
            data = json.loads(self.SESSION_FILE.read_text(encoding="utf-8"))
            cookies = data.get("cookies", {})
            payload = data.get("payload", {})
            if not payload:
                return False
            # Restore cookies into session
            for name, value in cookies.items():
                self.session.cookies.set(name, value, domain="ctyf.co.in")
            self._payload = payload
            # Quick check: does session still work?
            if self._session_still_valid():
                print("[Auth] ✓ Using saved session")
                return True
            print("[Auth] ✗ Saved session expired")
            return False
        except Exception as e:
            print(f"[Auth] Could not load session: {e}")
            return False

    def _session_still_valid(self) -> bool:
        """Ping the dashboard to verify session validity."""
        try:
            r = self.session.get(
                self.DASHBOARD_URL, timeout=15, allow_redirects=False
            )
            if r.status_code == 302:
                return False
            if r.status_code == 200 and "SiteLogin" not in r.text[:500]:
                return True
            return False
        except Exception:
            return False

    # ── Login & CAPTCHA ───────────────────────────────────────────────────────

    def _login(self, max_retries: int = 6):
        """Login to ctyf.co.in, solving CAPTCHA with ddddocr. Retries on wrong CAPTCHA."""
        creds = self._load_credentials()
        self._init_ocr()

        for attempt in range(1, max_retries + 1):
            print(f"[Auth] Login attempt {attempt}/{max_retries}...")

            # Fresh page load → fresh VIEWSTATE + CAPTCHA
            r = self.session.get(self.LOGIN_URL, timeout=20)
            soup = BeautifulSoup(r.text, "html.parser")

            def hidden_val(name: str) -> str:
                el = soup.find("input", {"name": name})
                return el["value"] if el else ""

            # Download and OCR the CAPTCHA image
            captcha_resp = self.session.get(self.CAPTCHA_URL, timeout=10)
            captcha_text = self._ocr.classification(captcha_resp.content).strip()
            # Strip spaces, keep alphanumeric only, force UPPERCASE
            captcha_text = "".join(c for c in captcha_text if c.isalnum()).upper()
            print(f"[Auth]   CAPTCHA read: '{captcha_text}'")

            # CAPTCHAs on this site are 5 chars — if OCR read < 4, it's definitely wrong
            if len(captcha_text) < 5:
                print(f"[Auth] ✗ OCR read too short ({len(captcha_text)} chars), retrying...")
                time.sleep(0.5)
                continue


            post_data = {
                "__VIEWSTATE":           hidden_val("__VIEWSTATE"),
                "__VIEWSTATEGENERATOR":  hidden_val("__VIEWSTATEGENERATOR"),
                "__EVENTVALIDATION":     hidden_val("__EVENTVALIDATION"),
                "txtUN":                 creds["username"],
                "txtPwd":                creds["password"],
                "txtCaptcha":            captcha_text,
                "btnLogin":              "Login",
            }

            r2 = self.session.post(
                self.LOGIN_URL,
                data=post_data,
                headers={"referer": self.LOGIN_URL, "content-type": "application/x-www-form-urlencoded"},
                timeout=20,
                allow_redirects=True,
            )

            # Check success: redirected away from login page
            if "SiteLogin" not in r2.url and "SiteLogin" not in r2.text[:600]:
                print("[Auth] ✓ Login successful!")
                self._extract_and_save_payload()
                return

            # Still on login page — could be wrong CAPTCHA or wrong credentials
            # Check specifically for credential error messages (not captcha errors)
            page_lower = r2.text.lower()
            if ("invalid username" in page_lower or
                "invalid password" in page_lower or
                "user not found" in page_lower or
                "account" in page_lower and "not exist" in page_lower):
                raise RuntimeError(
                    "Invalid username or password — check ctyf_config.json"
                )

            print(f"[Auth] ✗ CAPTCHA wrong ('{captcha_text}'), retrying...")
            time.sleep(0.8)

        raise RuntimeError(f"Login failed after {max_retries} attempts (CAPTCHA keeps failing). "
                           "Try running again — ddddocr usually succeeds within 2-3 tries.")

    # ── Payload extraction ────────────────────────────────────────────────────

    def _extract_and_save_payload(self):
        """Parse the auth cookie and extract api-locate.com payload keys."""
        raw_cookie = self.session.cookies.get(AUTH_COOKIE_NAME, "")

        if not raw_cookie:
            # Cookie might not be set yet — hit dashboard once
            self.session.get(self.DASHBOARD_URL, timeout=20)
            raw_cookie = self.session.cookies.get(AUTH_COOKIE_NAME, "")

        if not raw_cookie:
            raise RuntimeError(
                f"Cookie '{AUTH_COOKIE_NAME}' not found after login. "
                "The login may have partially failed."
            )

        # Cookie format: "encKey1=encVal1&encKey2=encVal2&..."
        cookie_pairs = {}
        for part in raw_cookie.split("&"):
            if "=" in part:
                k, _, v = part.partition("=")
                cookie_pairs[k.strip()] = v.strip()

        payload = {}
        for cookie_key, payload_key in COOKIE_KEY_TO_PAYLOAD.items():
            val = cookie_pairs.get(cookie_key)
            if val:
                payload[payload_key] = val

        # gisrls duplicates mzxkwl
        if "mzxkwl" in payload:
            payload["gisrls"] = payload["mzxkwl"]

        if len(payload) < 5:
            # Fallback: if key mapping didn't work (new session structure),
            # use all cookie values in order
            print("[Auth] ⚠ Key mapping incomplete — using positional fallback")
            vals = list(cookie_pairs.values())
            keys = ["kangve", "wwacgg", "mzxkwl", "gisrls", "luuoza", "yztrxb", "megkef"]
            payload = {k: vals[i] for i, k in enumerate(keys) if i < len(vals)}

        self._payload = payload

        # Persist
        session_data = {
            "cookies": {c.name: c.value for c in self.session.cookies},
            "payload": payload,
        }
        self.SESSION_FILE.write_text(json.dumps(session_data, indent=2), encoding="utf-8")
        print(f"[Auth] ✓ Session + payload saved to {self.SESSION_FILE}")

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _load_credentials(self) -> dict:
        if not self.CONFIG_FILE.exists():
            raise FileNotFoundError(
                f"\nCredentials file not found: {self.CONFIG_FILE}\n"
                f"Create it with:\n"
                f'  {{"username": "your_login", "password": "your_password"}}'
            )
        creds = json.loads(self.CONFIG_FILE.read_text(encoding="utf-8"))
        if creds.get("username") == "YOUR_USERNAME_HERE":
            raise ValueError(
                f"Please edit {self.CONFIG_FILE} and replace YOUR_USERNAME_HERE / YOUR_PASSWORD_HERE "
                "with your actual ctyf.co.in credentials."
            )
        return creds

    def _init_ocr(self):
        if self._ocr is None:
            try:
                import ddddocr
                self._ocr = ddddocr.DdddOcr(show_ad=False)
            except ImportError:
                raise ImportError(
                    "ddddocr not installed. Run:  pip install ddddocr"
                )


# ── Quick test ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    auth = CtyFAuth()
    payload = auth.get_payload()
    print("\nPayload keys ready:")
    for k, v in payload.items():
        print(f"  {k}: {v[:12]}...")
