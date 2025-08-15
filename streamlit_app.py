
import os
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
st.caption("Lock before you dial. Everyone sees locks instantly across brands. Duplicate checks: exact email/phone, domain match, fuzzy company (optional).")

BRANDS = ["Dartmouth Partners","Catalyst Partners","Pure Search","Other"]

# ----------------------------
# Helpers
# ----------------------------
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
    """Safe read of query param for modern Streamlit API."""
    try:
        qp = st.query_params
        v = qp.get(key, "")
        if isinstance(v, list):
            return v[0] if v else ""
        return v or ""
    except Exception:
        return ""

def set_qp(**kwargs):
    """Safe set of query params; clears keys when value is empty."""
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

# ----------------------------
# SETTINGS (SIDEBAR) + PROFILE
# ----------------------------
with st.sidebar:
    st.header("Settings")

    # Prefer SHEET_URL from Secrets; support both root-level and inside gcp_service_account, then fall back to env.
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
        sheet_url = default_url  # Locked in by admin
        st.caption("Sheet is preconfigured by the admin.")
    else:
        sheet_url = st.text_input(
            "Google Sheet URL (admin only)",
            value="",
            help="Set via Secrets as SHEET_URL so users never see this."
        )

    tz_name = st.selectbox("Timezone", ["Europe/London", "UTC"], index=0)
    fuzzy_threshold = st.slider("Fuzzy company match threshold", min_value=70, max_value=95, value=82, help="Higher = stricter matches")

    st.markdown("---")
    st.subheader("Your profile (Remember me)")

    # Initialize profile state from URL params or defaults
    st.session_state.setdefault("profile_name", get_qp("name"))
    qp_brand = get_qp("brand")
    st.session_state.setdefault("profile_brand", qp_brand if qp_brand in BRANDS else BRANDS[0])

    # Keep last saved to detect changes
    st.session_state.setdefault("prev_profile_name", st.session_state["profile_name"])
    st.session_state.setdefault("prev_profile_brand", st.session_state["profile_brand"])

    # Editable profile inputs
    profile_name = st.text_input("Your Name (default)", key="profile_name")
    profile_brand = st.selectbox("Your Brand (default)", options=BRANDS, key="profile_brand")

    # If either changed, immediately persist to URL (so bookmarks work), and update prev_* trackers
    if (st.session_state["profile_name"] != st.session_state["prev_profile_name"]) or (st.session_state["profile_brand"] != st.session_state["prev_profile_brand"]):
        set_qp(name=st.session_state["profile_name"], brand=st.session_state["profile_brand"])
        st.session_state["prev_profile_name"] = st.session_state["profile_name"]
        st.session_state["prev_profile_brand"] = st.session_state["profile_brand"]
        st.caption("✅ Profile saved to this URL. Bookmark it to keep your defaults.")

    # Quick action: sync profile into the form right now
    if st.button("Reset to profile now"):
        st.session_state["locked_by"] = st.session_state["profile_name"]
        st.session_state["brand"] = st.session_state["profile_brand"]
        st.success("Form defaults synced from your profile.")

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
            reset_today = st.button("🧹 Clear TODAY's locks (keep history)", use_container_width=True)
            archive_today = st.button("📦 Archive TODAY to 'Archive' + Clear", use_container_width=True)
        with col_b:
            reset_all = st.button("🧨 Reset ALL locks (keep header)", use_container_width=True)
            archive_all = st.button("📦 Archive ALL to 'Archive' + Clear", use_container_width=True)
        st.caption("Archive buttons copy rows to an 'Archive' worksheet before clearing.")
    else:
        st.info("Enter Admin PIN to enable reset/archival actions.")

# ----------------------------
# AUTH
# ----------------------------
def get_credentials():
    if hasattr(st, "secrets") and "gcp_service_account" in st.secrets:
        service_account_info = dict(st.secrets["gcp_service_account"])
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        return Credentials.from_service_account_info(service_account_info, scopes=scopes)
    if os.path.exists("service_account.json"):
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
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
        ws.update(
            "A1:I1",
            [["Timestamp","Date","Company","Contact Name","Email","Phone","Brand","Locked By","Notes"]],
        )
    return ws, sh

def find_duplicates(df, company_n, email_n, phone_n, domain, fuzzy_threshold):
    hits = []
    if df.empty:
        return hits, pd.DataFrame(columns=df.columns)
    if email_n:
        exact_email = df[df["_email_n"] == email_n]
        if not exact_email.empty:
            hits.append(("Exact email", exact_email))
    if phone_n:
        exact_phone = df[df["_phone_n"] == phone_n]
        if not exact_phone.empty:
            hits.append(("Exact phone", exact_phone))
    if domain:
        dom_df = df[df["_domain"] == domain]
        if not dom_df.empty:
            hits.append((f"Same email domain @{domain}", dom_df))
    if company_n and HAS_RAPIDFUZZ:
        uniq_companies = df["_company_n"].dropna().unique().tolist()
        matched_vals = []
        for comp in uniq_companies:
            if not comp:
                continue
            score = fuzz.token_set_ratio(company_n, comp)
            if score >= fuzzy_threshold:
                matched_vals.append(comp)
        if matched_vals:
            fuzzy_df = df[df["_company_n"].isin(set(matched_vals))]
            if not fuzzy_df.empty:
                hits.append((f"Fuzzy company ≥{fuzzy_threshold}", fuzzy_df))
    if hits:
        parts = [h[1] for h in hits]
        combined = pd.concat(parts, axis=0).drop_duplicates()
        combined = combined.sort_values("Timestamp", ascending=False)
    else:
        combined = pd.DataFrame(columns=df.columns)
    return hits, combined

# ----------------------------
# Early exit if no sheet
# ----------------------------
if 'confirm_sig' not in st.session_state:
    st.session_state['confirm_sig'] = None
if 'confirm_ready' not in st.session_state:
    st.session_state['confirm_ready'] = False

for key in ["company","contact_name","email","phone","notes","brand","locked_by"]:
    st.session_state.setdefault(key, "")

# Prefill from profile if empty
if not st.session_state.get("locked_by") and st.session_state.get("profile_name"):
    st.session_state["locked_by"] = st.session_state["profile_name"]
if not st.session_state.get("brand"):
    st.session_state["brand"] = st.session_state.get("profile_brand", BRANDS[0])

if not default_url and 'sheet_url' not in locals():
    sheet_url = ""

if not sheet_url:
    st.warning("Admin: set SHEET_URL in Secrets so users aren't asked for it.")
    st.stop()

# ----------------------------
# Load existing data
# ----------------------------
try:
    ws, sh = open_sheet(sheet_url)
except Exception as e:
    st.error(f"Could not open sheet. Check URL, sharing and credentials. Details: {e}")
    st.stop()

rows = ws.get_all_records()
df = pd.DataFrame(rows, columns=["Timestamp","Date","Company","Contact Name","Email","Phone","Brand","Locked By","Notes"])

if not df.empty:
    df["Timestamp"] = pd.to_datetime(df["Timestamp"], errors="coerce")
    df["_company_n"] = df["Company"].astype(str).apply(normalize_text)
    df["_email_n"] = df["Email"].astype(str).apply(normalize_text)
    df["_domain"] = df["Email"].astype(str).apply(email_domain)
    df["_phone_n"] = df["Phone"].astype(str).apply(normalize_phone)
else:
    df = pd.DataFrame(columns=["Timestamp","Date","Company","Contact Name","Email","Phone","Brand","Locked By","Notes",
                               "_company_n","_email_n","_domain","_phone_n"])

# ----------------------------
# Admin reset/archive actions
# ----------------------------
def get_or_create_archive(sh):
    try:
        arch = sh.worksheet("Archive")
    except gspread.WorksheetNotFound:
        arch = sh.add_worksheet(title="Archive", rows=4000, cols=12)
        arch.update("A1:I1", [["Timestamp","Date","Company","Contact Name","Email","Phone","Brand","Locked By","Notes"]])
    return arch

def admin_clear_today():
    today_str = now_in_tz().strftime("%Y-%m-%d")
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
    today_str = now_in_tz().strftime("%Y-%m-%d")
    values = ws.get_all_values()
    rows_to_archive = []
    row_numbers = []
    for idx, row in enumerate(values[1:], start=2):
        if len(row) >= 2 and row[1] == today_str:
            rows_to_archive.append(row[:9])
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
    data_rows = [row[:9] for row in values[1:]]
    arch = get_or_create_archive(sh)
    arch.append_rows(data_rows, value_input_option="USER_ENTERED")
    ws.delete_rows(2, ws.row_count)
    return f"Archived and cleared {len(data_rows)} row(s)."

if 'is_admin' in locals() and is_admin:
    if 'reset_today' in locals() and reset_today:
        st.success(admin_clear_today()); st.experimental_rerun()
    if 'reset_all' in locals() and reset_all:
        st.success(admin_clear_all()); st.experimental_rerun()
    if 'archive_today' in locals() and archive_today:
        st.success(admin_archive_today_and_clear()); st.experimental_rerun()
    if 'archive_all' in locals() and archive_all:
        st.success(admin_archive_all_and_clear()); st.experimental_rerun()

# ----------------------------
# Form: Lock a contact
# ----------------------------
st.subheader("Lock a Contact")

def clear_form_fields():
    for key in ["company","contact_name","email","phone","notes"]:
        st.session_state[key] = ""

with st.form("lock_form", clear_on_submit=False):
    live_alert = st.empty()

    col1, col2, col3 = st.columns([1.3,1,1])
    with col1:
        company = st.text_input("Company *", key="company")
        contact_name = st.text_input("Contact Name *", key="contact_name")
        email = st.text_input("Email (recommended)", key="email")
        phone = st.text_input("Phone", key="phone")
        notes = st.text_area("Notes (optional)", height=72, key="notes")

        st.markdown(" ")
        if st.form_submit_button("🧽 Clear form (Company/Contact/Email/Phone/Notes)", help="Does not clear your profile, brand, or name."):
            clear_form_fields(); st.experimental_rerun()

    with col2:
        brand = st.selectbox("Your Brand *", BRANDS, key="brand")
        if not st.session_state.get("locked_by") and st.session_state.get("profile_name"):
            st.session_state["locked_by"] = st.session_state["profile_name"]
        locked_by = st.text_input("Your Name *", key="locked_by")
        st.markdown(" "); st.markdown("**Duplicate Check (live)**")

    with col3:
        st.markdown("**Match Signals**")
        check_company = normalize_text(st.session_state["company"])
        check_email = normalize_text(st.session_state["email"])
        check_domain = email_domain(st.session_state["email"])
        check_phone = normalize_phone(st.session_state["phone"])

        live_hits, live_combined = find_duplicates(df, check_company, check_email, check_phone, check_domain, fuzzy_threshold)

        if live_hits:
            st.error("⚠ Potential duplicate(s) detected while typing. Review below.")
            for label, sub in live_hits:
                st.markdown(f"**{label}**")
                st.dataframe(
                    sub[["Timestamp","Company","Contact Name","Email","Phone","Brand","Locked By","Notes"]].sort_values("Timestamp", ascending=False),
                    use_container_width=True
                )
        else:
            st.success("✅ No duplicates found yet on company/email/phone/domain checks.")

    if live_hits:
        live_alert.error("🚨 Potential duplicate(s) detected based on what you've typed. Please review before locking.")

    submitted = st.form_submit_button("🔒 Lock Contact")

    if submitted:
        company = st.session_state["company"]
        contact_name = st.session_state["contact_name"]
        email = st.session_state["email"]
        phone = st.session_state["phone"]
        notes = st.session_state["notes"]
        brand = st.session_state["brand"]
        locked_by = st.session_state["locked_by"]

        if not company or not contact_name or not brand or not locked_by:
            st.warning("Please fill in all *required* fields.")
        elif not email and not phone:
            st.warning("Please provide at least an Email or a Phone number.")
        else:
            hits, combined = find_duplicates(df, normalize_text(company), normalize_text(email), normalize_phone(phone), email_domain(email), fuzzy_threshold)
            sig = f"{normalize_text(email)}|{normalize_phone(phone)}|{normalize_text(company)}"

            if hits and (st.session_state.get('confirm_sig') != sig or not st.session_state.get('confirm_ready', False)):
                st.session_state['confirm_sig'] = sig
                st.session_state['confirm_ready'] = True
                st.error("⚠ Potential duplicate(s) detected — please review the matches above. "
                         "If you still want to proceed, click **Lock Contact** again to confirm.")
                if not combined.empty:
                    st.dataframe(
                        combined[["Timestamp","Company","Contact Name","Email","Phone","Brand","Locked By","Notes"]],
                        use_container_width=True
                    )
            else:
                try:
                    ts = now_in_tz()
                    date_str = ts.strftime("%Y-%m-%d")
                    ts_iso = ts.strftime("%Y-%m-%d %H:%M:%S")
                    new_row = [ts_iso, date_str, company.strip(), contact_name.strip(), email.strip(), phone.strip(), brand, locked_by.strip(), notes.strip()]
                    ws.append_row(new_row, value_input_option="USER_ENTERED")
                    st.success("Contact locked for today. Visible to all teams now.")
                    st.session_state['confirm_sig'] = None
                    st.session_state['confirm_ready'] = False
                    clear_form_fields(); st.experimental_rerun()
                except Exception as e:
                    st.error(f"Failed to save. Details: {e}")

# ----------------------------
# Today view
# ----------------------------
st.subheader("Today’s Locks (Live)")
with st.expander("Filters", expanded=True):
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        q_company = st.text_input("Filter by Company")
    with c2:
        q_email = st.text_input("Filter by Email")
    with c3:
        brand_filter = st.multiselect("Filter by Brand", BRANDS)
    with c4:
        q_phone = st.text_input("Filter by Phone (digits only)")

if not df.empty:
    today_str = now_in_tz().strftime("%Y-%m-%d")
    today_df = df[df["Date"] == today_str].copy()

    if q_company:
        qn = normalize_text(q_company)
        today_df = today_df[today_df["_company_n"].str.contains(qn, na=False)]
    if q_email:
        qn = normalize_text(q_email)
        today_df = today_df[today_df["_email_n"].str.contains(qn, na=False)]
    if brand_filter:
        today_df = today_df[today_df["Brand"].isin(brand_filter)]
    if q_phone:
        qn = normalize_phone(q_phone)
        today_df = today_df[today_df["_phone_n"].str.contains(qn, na=False)]

    if not today_df.empty:
        today_df["Dup Today?"] = (
            today_df.duplicated(subset=["_email_n"], keep="first") |
            today_df.duplicated(subset=["_phone_n"], keep="first") |
            today_df.duplicated(subset=["_company_n"], keep="first")
        )

    st.dataframe(
        today_df[["Timestamp","Company","Contact Name","Email","Phone","Brand","Locked By","Notes","Dup Today?"]]
        .sort_values("Timestamp", ascending=False),
        use_container_width=True
    )
else:
    st.info("No locks yet.")

st.markdown("---")
st.caption("Signals used: exact email/phone, same email domain, fuzzy company match (RapidFuzz).")
