
import os
import hashlib
from datetime import datetime, timezone
import pandas as pd
import streamlit as st

# Google Sheets
import gspread
from google.oauth2.service_account import Credentials

# Fuzzy matching (optional)
try:
    from rapidfuzz import fuzz
    HAS_RAPIDFUZZ = True
except Exception:
    HAS_RAPIDFUZZ = False

st.set_page_config(page_title="BD Day – Contact Lockout", page_icon="📞", layout="wide")
st.title("📞 BD Day – Contact Lockout")
st.caption("Lock before you dial. Everyone sees locks instantly across brands. Duplicate checks: exact email/phone (via hashes) and fuzzy company (82). No emails or phone numbers are stored.")

# ----------------------------
# Constants & helpers
# ----------------------------
BRANDS = ["Dartmouth Partners", "Catalyst Partners", "Pure Search", "Other"]
FUZZY_THRESHOLD = 82  # fixed

def now_in_tz(tz="Europe/London"):
    try:
        import zoneinfo
        z = zoneinfo.ZoneInfo(tz)
        return datetime.now(tz=z)
    except Exception:
        return datetime.now(timezone.utc)

def normalize_text(x: str) -> str:
    if not x:
        return ""
    return " ".join(str(x).strip().lower().split())

def normalize_phone(p: str) -> str:
    if not p:
        return ""
    return "".join(ch for ch in str(p) if ch.isdigit())

def email_domain(email: str) -> str:
    if not email:
        return ""
    e = str(email).strip().lower()
    if "@" in e:
        return e.split("@", 1)[1]
    return ""

def get_qp(key: str) -> str:
    try:
        qp = st.query_params
        v = qp.get(key, "")
        if isinstance(v, list):
            return v[0] if v else ""
        return v or ""
    except Exception:
        return ""

def set_qp(**kwargs):
    try:
        qp = dict(st.query_params)
        for k, v in kwargs.items():
            if v:
                qp[k] = v
            else:
                qp.pop(k, None)
        st.query_params.clear()
        st.query_params.update(qp)
    except Exception:
        pass

def get_salt() -> str:
    # Prefer secrets, then env var; default to fixed string (encourage setting a secret)
    salt = ""
    if hasattr(st, "secrets"):
        salt = st.secrets.get("HASH_SALT", "")
        if not salt and "gcp_service_account" in st.secrets:
            try:
                salt = st.secrets["gcp_service_account"].get("HASH_SALT", "")
            except Exception:
                salt = ""
    if not salt:
        salt = os.environ.get("HASH_SALT", "")
    if not salt:
        salt = "set-a-strong-random-salt-in-secrets"  # fallback; recommend replacing
    return salt

def sha256_hex(value: str) -> str:
    if not value:
        return ""
    h = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return h

# ----------------------------
# Sidebar (minimal)
# ----------------------------
with st.sidebar:
    st.header("Settings")
    # Prefer SHEET_URL from Secrets; support both root-level and inside gcp_service_account, then env.
    default_url = ""
    if hasattr(st, "secrets"):
        default_url = st.secrets.get("SHEET_URL", "") or default_url
        if not default_url and "gcp_service_account" in st.secrets:
            try:
                default_url = st.secrets["gcp_service_account"].get("SHEET_URL", "")
            except Exception:
                default_url = default_url
    if not default_url:
        default_url = os.environ.get("SHEET_URL", "")

    if default_url:
        sheet_url = default_url
        st.caption("Sheet is preconfigured by the admin.")
    else:
        sheet_url = st.text_input("Google Sheet URL (admin only)", value="", help="Set via Secrets as SHEET_URL so users never see this.")

    tz_name = st.selectbox("Timezone", ["Europe/London", "UTC"], index=0)

    # --- Admin tools ---
    st.markdown("---")
    st.subheader("Admin tools")
    admin_pin_secret = ""
    if hasattr(st, "secrets"):
        admin_pin_secret = st.secrets.get("ADMIN_PIN", "")
        if not admin_pin_secret and "gcp_service_account" in st.secrets:
            try:
                admin_pin_secret = st.secrets["gcp_service_account"].get("ADMIN_PIN", "")
            except Exception:
                admin_pin_secret = ""
    if not admin_pin_secret:
        admin_pin_secret = os.environ.get("ADMIN_PIN", "")

    admin_entered = st.text_input("Admin PIN", value="", type="password", help="Set ADMIN_PIN in Secrets to enable resets.")
    is_admin = bool(admin_pin_secret) and admin_entered == admin_pin_secret

    if is_admin:
        st.success("Admin mode enabled")
        col_a, col_b = st.columns(2)
        with col_a:
            reset_today = st.button("🧹 Clear TODAY's locks", use_container_width=True)
            archive_today = st.button("📦 Archive TODAY + Clear", use_container_width=True)
        with col_b:
            reset_all = st.button("🧨 Reset ALL locks", use_container_width=True)
            archive_all = st.button("📦 Archive ALL + Clear", use_container_width=True)
        st.caption("Archive buttons copy rows to an 'Archive' worksheet before clearing.")
    else:
        st.info("Enter Admin PIN to enable reset/archival actions.")

# ----------------------------
# Auth & Sheets
# ----------------------------
def get_credentials():
    if hasattr(st, "secrets") and "gcp_service_account" in st.secrets:
        service_account_info = dict(st.secrets["gcp_service_account"])
        scopes = ["https://www.googleapis.com/auth/spreadsheets","https://www.googleapis.com/auth/drive"]
        return Credentials.from_service_account_info(service_account_info, scopes=scopes)
    if os.path.exists("service_account.json"):
        scopes = ["https://www.googleapis.com/auth/spreadsheets","https://www.googleapis.com/auth/drive"]
        return Credentials.from_service_account_file("service_account.json", scopes=scopes)
    raise RuntimeError("No credentials found. Add Streamlit secret `gcp_service_account` or upload service_account.json.")

def get_gspread_client():
    creds = get_credentials()
    return gspread.authorize(creds)

@st.cache_resource(show_spinner=False)
def open_sheet(url: str):
    gc = get_gspread_client()
    sh = gc.open_by_url(url)
    try:
        ws = sh.worksheet("Locks")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title="Locks", rows=4000, cols=12)
        ws.update("A1:H1", [[
            "Timestamp","Date","Company","Contact Name","Brand","Locked By","Notes","EmailHash","PhoneHash"
        ]])
    else:
        # Attempt to migrate: remove Email/Phone PII columns if present; ensure hash columns exist
        try:
            headers = ws.row_values(1)
            # Add hash columns if missing
            if "EmailHash" not in headers:
                ws.update_cell(1, len(headers)+1, "EmailHash")
                headers.append("EmailHash")
            if "PhoneHash" not in headers:
                ws.update_cell(1, len(headers)+1, "PhoneHash")
                headers.append("PhoneHash")
            # Remove PII columns if present
            # We delete from right to left to keep indices valid
            if "Phone" in headers:
                idx = headers.index("Phone") + 1
                ws.delete_columns(idx)
            if "Email" in headers:
                idx = ws.row_values(1).index("Email") + 1
                ws.delete_columns(idx)
        except Exception:
            pass
    return ws, sh

# ----------------------------
# Session state init
# ----------------------------
if "confirm_sig" not in st.session_state:
    st.session_state["confirm_sig"] = None
if "confirm_ready" not in st.session_state:
    st.session_state["confirm_ready"] = False

for key in ["company", "contact_name", "email", "phone", "notes"]:
    st.session_state.setdefault(key, "")

# form reset flag (pre-render clearing)
st.session_state.setdefault("_do_clear_form", False)

# Pull profile from URL if present (bypass popup on bookmarked link)
if "profile_name" not in st.session_state:
    maybe_name = get_qp("name")
    st.session_state["profile_name"] = maybe_name if isinstance(maybe_name, str) else ""
if "profile_brand" not in st.session_state:
    maybe_brand = get_qp("brand")
    st.session_state["profile_brand"] = maybe_brand if maybe_brand in BRANDS else ""

# Blocking profile popup if profile not yet set
if not st.session_state["profile_name"] or not st.session_state["profile_brand"]:
    st.markdown("### 👋 Welcome! Set your profile")
    st.info("Please enter your **Name** and select your **Brand** to continue.")
    with st.form("profile_setup", clear_on_submit=False):
        p_name = st.text_input("Your Name *", value="")
        p_brand = st.selectbox("Your Brand *", BRANDS, index=0)
        ok = st.form_submit_button("Save profile")
        if ok:
            if not p_name.strip():
                st.warning("Name is required.")
            elif not p_brand:
                st.warning("Brand is required.")
            else:
                st.session_state["profile_name"] = p_name.strip()
                st.session_state["profile_brand"] = p_brand
                set_qp(name=st.session_state["profile_name"], brand=st.session_state["profile_brand"])
                st.success("Profile saved. You can start locking contacts.")
                st.rerun()
    st.stop()

# From here, profile exists
locked_by_profile = st.session_state["profile_name"]
brand_profile = st.session_state["profile_brand"]

# Sheet URL required
if not default_url and "sheet_url" not in locals():
    sheet_url = ""
if not sheet_url:
    st.warning("Admin: set SHEET_URL in Secrets so users aren't asked for it.")
    st.stop()

# Open and read data
try:
    ws, sh = open_sheet(sheet_url)
except Exception as e:
    st.error(f"Could not open sheet. Check URL, sharing and credentials. Details: {e}")
    st.stop()

rows = ws.get_all_records()
# Backward compatibility: tolerate presence/absence of hash columns in historical data
expected_cols = ["Timestamp","Date","Company","Contact Name","Brand","Locked By","Notes","EmailHash","PhoneHash"]
df = pd.DataFrame(rows)
for col in expected_cols:
    if col not in df.columns:
        df[col] = ""

if not df.empty:
    df["Timestamp"] = pd.to_datetime(df["Timestamp"], errors="coerce")
    df["_company_n"] = df["Company"].astype(str).apply(normalize_text)
    df["_email_h"] = df["EmailHash"].astype(str)
    df["_phone_h"] = df["PhoneHash"].astype(str)
else:
    df = pd.DataFrame(columns=["Timestamp","Date","Company","Contact Name","Brand","Locked By","Notes","EmailHash","PhoneHash",
                               "_company_n","_email_h","_phone_h"])

# ----------------------------
# Admin actions
# ----------------------------
def get_or_create_archive(sh):
    try:
        arch = sh.worksheet("Archive")
    except gspread.WorksheetNotFound:
        arch = sh.add_worksheet(title="Archive", rows=4000, cols=12)
        arch.update("A1:G1", [["Timestamp","Date","Company","Contact Name","Brand","Locked By","Notes"]])
    return arch

def admin_clear_today():
    today_str = now_in_tz(tz_name).strftime("%Y-%m-%d")
    values = ws.get_all_values()
    to_delete = []
    for idx, row in enumerate(values[1:], start=2):
        if len(row) >= 2 and row[1] == today_str:
            to_delete.append(idx)
    if not to_delete:
        return "No rows for today to delete."
    for r in reversed(to_delete):
        ws.delete_rows(r)
    return f"Deleted {len(to_delete)} row(s) for today ({today_str})."

def admin_clear_all():
    ws.delete_rows(2, ws.row_count)
    return "All locks cleared (header preserved)."

def admin_archive_today_and_clear():
    today_str = now_in_tz(tz_name).strftime("%Y-%m-%d")
    values = ws.get_all_values()
    rows_to_archive = []
    row_numbers = []
    for idx, row in enumerate(values[1:], start=2):
        if len(row) >= 2 and row[1] == today_str:
            rows_to_archive.append(row[:7])  # up to Notes
            row_numbers.append(idx)
    if not rows_to_archive:
        return "No rows for today to archive."
    arch = get_or_create_archive(sh)
    arch.append_rows(rows_to_archive, value_input_option="USER_ENTERED")
    for r in reversed(row_numbers):
        ws.delete_rows(r)
    return f"Archived and cleared {len(rows_to_archive)} row(s) for today ({today_str})."

def admin_archive_all_and_clear():
    values = ws.get_all_values()
    if len(values) <= 1:
        return "No data rows to archive."
    data_rows = [row[:7] for row in values[1:]]  # up to Notes
    arch = get_or_create_archive(sh)
    arch.append_rows(data_rows, value_input_option="USER_ENTERED")
    ws.delete_rows(2, ws.row_count)
    return f"Archived and cleared {len(data_rows)} row(s)."

if is_admin:
    if 'reset_today' in locals() and reset_today:
        st.success(admin_clear_today()); st.rerun()
    if 'reset_all' in locals() and reset_all:
        st.success(admin_clear_all()); st.rerun()
    if 'archive_today' in locals() and archive_today:
        st.success(admin_archive_today_and_clear()); st.rerun()
    if 'archive_all' in locals() and archive_all:
        st.success(admin_archive_all_and_clear()); st.rerun()

# ----------------------------
# Duplicate finder (using hashes + fuzzy company)
# ----------------------------
def find_duplicates(df, company_n, email_hash, phone_hash):
    hits = []
    if df.empty:
        return hits, pd.DataFrame(columns=df.columns)
    if email_hash:
        exact_email = df[df["_email_h"] == email_hash]
        if not exact_email.empty:
            hits.append(("Exact email (hashed)", exact_email))
    if phone_hash:
        exact_phone = df[df["_phone_h"] == phone_hash]
        if not exact_phone.empty:
            hits.append(("Exact phone (hashed)", exact_phone))
    if company_n and HAS_RAPIDFUZZ:
        uniq_companies = df["_company_n"].dropna().unique().tolist()
        matched_vals = []
        for comp in uniq_companies:
            if not comp:
                continue
            score = fuzz.token_set_ratio(company_n, comp)
            if score >= FUZZY_THRESHOLD:
                matched_vals.append(comp)
        if matched_vals:
            fuzzy_df = df[df["_company_n"].isin(set(matched_vals))]
            if not fuzzy_df.empty:
                hits.append((f"Fuzzy company ≥{FUZZY_THRESHOLD}", fuzzy_df))
    if hits:
        parts = [h[1] for h in hits]
        combined = pd.concat(parts, axis=0).drop_duplicates()
        combined = combined.sort_values("Timestamp", ascending=False)
    else:
        combined = pd.DataFrame(columns=df.columns)
    return hits, combined

# ----------------------------
# Pre-render form clear (before widgets)
# ----------------------------
if st.session_state.get("_do_clear_form"):
    for _k in ["company", "contact_name", "email", "phone", "notes"]:
        st.session_state[_k] = ""
    st.session_state["_do_clear_form"] = False

def request_clear_form():
    st.session_state["_do_clear_form"] = True

# ----------------------------
# Main form layout: two columns
# ----------------------------
st.subheader("Lock a Contact")

with st.form("lock_form", clear_on_submit=False):
    live_alert = st.empty()
    left, right = st.columns([1.3, 1])

    with left:
        company = st.text_input("Company *", key="company")
        contact_name = st.text_input("Contact Name *", key="contact_name")
        email = st.text_input("Email (used for duplicate check only; not stored)", key="email")
        phone = st.text_input("Phone (used for duplicate check only; not stored)", key="phone")
        notes = st.text_area("Notes (optional)", height=72, key="notes")

        st.markdown(" ")
        if st.form_submit_button("🧽 Clear form (Company/Contact/Email/Phone/Notes)"):
            request_clear_form()
            st.rerun()

    with right:
        st.markdown("**Match Signals**")
        # Compute normalized + hashed values for live checks (not stored)
        salt = get_salt()
        check_company = normalize_text(st.session_state["company"])
        check_email = normalize_text(st.session_state["email"])
        check_phone = normalize_phone(st.session_state["phone"])
        email_hash = sha256_hex(salt + check_email) if check_email else ""
        phone_hash = sha256_hex(salt + check_phone) if check_phone else ""

        live_hits, live_combined = find_duplicates(df, check_company, email_hash, phone_hash)

        if live_hits:
            st.error("⚠ Potential duplicate(s) detected while typing. Review below.")
            for label, sub in live_hits:
                st.markdown(f"**{label}**")
                st.dataframe(
                    sub[["Timestamp", "Company", "Contact Name", "Brand", "Locked By", "Notes"]].sort_values("Timestamp", ascending=False),
                    use_container_width=True
                )
        else:
            st.success("✅ No duplicates found yet on company/email/phone checks.")

    if live_hits:
        live_alert.error("🚨 Potential duplicate(s) detected based on what you've typed. Please review before locking.")

    submitted = st.form_submit_button("🔒 Lock Contact")

    if submitted:
        # Pull from session and profile
        company = st.session_state["company"]
        contact_name = st.session_state["contact_name"]
        email = st.session_state["email"]
        phone = st.session_state["phone"]
        notes = st.session_state["notes"]
        brand = brand_profile
        locked_by = locked_by_profile

        # Validations
        if not locked_by or not brand:
            st.error("Profile missing. Please refresh and complete your profile popup.")
        elif not company or not contact_name:
            st.warning("Please fill in all *required* fields (Company, Contact Name).")
        elif not email and not phone:
            st.warning("Please provide at least an Email or a Phone number (used for duplicate check only).")
        else:
            salt = get_salt()
            email_hash = sha256_hex(salt + normalize_text(email)) if email else ""
            phone_hash = sha256_hex(salt + normalize_phone(phone)) if phone else ""

            hits, combined = find_duplicates(df, normalize_text(company), email_hash, phone_hash)
            sig = f"{email_hash}|{phone_hash}|{normalize_text(company)}"

            if hits and (st.session_state.get("confirm_sig") != sig or not st.session_state.get("confirm_ready", False)):
                st.session_state["confirm_sig"] = sig
                st.session_state["confirm_ready"] = True
                st.error("⚠ Potential duplicate(s) detected — please review the matches on the right. "
                         "If you still want to proceed, click **Lock Contact** again to confirm.")
                if not combined.empty:
                    st.dataframe(
                        combined[["Timestamp", "Company", "Contact Name", "Brand", "Locked By", "Notes"]],
                        use_container_width=True
                    )
            else:
                try:
                    ts = now_in_tz(tz_name)
                    date_str = ts.strftime("%Y-%m-%d")
                    ts_iso = ts.strftime("%Y-%m-%d %H:%M:%S")
                    # Append WITHOUT storing PII (email/phone); store only salted hashes
                    new_row = [
                        ts_iso, date_str, company.strip(), contact_name.strip(),
                        brand, locked_by.strip(), notes.strip(),
                        email_hash, phone_hash
                    ]
                    # Ensure header shape matches:
                    # ["Timestamp","Date","Company","Contact Name","Brand","Locked By","Notes","EmailHash","PhoneHash"]
                    ws.append_row(new_row, value_input_option="USER_ENTERED")
                    st.success("Contact locked for today. Visible to all teams now (no email/phone stored).")
                    st.session_state["confirm_sig"] = None
                    st.session_state["confirm_ready"] = False
                    request_clear_form()
                    st.rerun()
                except Exception as e:
                    st.error(f"Failed to save. Details: {e}")

# ----------------------------
# Today view (no email/phone columns)
# ----------------------------
st.subheader("Today’s Locks (Live)")
with st.expander("Filters", expanded=True):
    c1, c2, c3 = st.columns(3)
    with c1:
        q_company = st.text_input("Filter by Company")
    with c2:
        q_contact = st.text_input("Filter by Contact Name")
    with c3:
        brand_filter = st.multiselect("Filter by Brand", BRANDS)

if not df.empty:
    today_str = now_in_tz(tz_name).strftime("%Y-%m-%d")
    today_df = df[df["Date"] == today_str].copy()

    if q_company:
        qn = normalize_text(q_company)
        today_df = today_df[today_df["_company_n"].str.contains(qn, na=False)]
    if q_contact:
        qn = normalize_text(q_contact)
        if "Contact Name" in today_df.columns:
            today_df["_contact_n"] = today_df["Contact Name"].astype(str).apply(normalize_text)
            today_df = today_df[today_df["_contact_n"].str.contains(qn, na=False)]
    if brand_filter:
        today_df = today_df[today_df["Brand"].isin(brand_filter)]

    # Dup mark (today) using hashes and company
    if not today_df.empty:
        # Build temp normalized cols if missing
        if "_company_n" not in today_df.columns:
            today_df["_company_n"] = today_df["Company"].astype(str).apply(normalize_text)
        if "_email_h" not in today_df.columns and "EmailHash" in today_df.columns:
            today_df["_email_h"] = today_df["EmailHash"].astype(str)
        if "_phone_h" not in today_df.columns and "PhoneHash" in today_df.columns:
            today_df["_phone_h"] = today_df["PhoneHash"].astype(str)

        today_df["Dup Today?"] = (
            today_df.duplicated(subset=["_email_h"], keep="first") |
            today_df.duplicated(subset=["_phone_h"], keep="first") |
            today_df.duplicated(subset=["_company_n"], keep="first")
        )

    st.dataframe(
        today_df[["Timestamp","Company","Contact Name","Brand","Locked By","Notes","Dup Today?"]]
        .sort_values("Timestamp", ascending=False),
        use_container_width=True
    )
else:
    st.info("No locks yet.")

st.markdown("---")
st.caption("No email or phone numbers are stored. Duplicate checks use salted hashes of email/phone and fuzzy company match (82).")
