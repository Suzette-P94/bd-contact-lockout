import streamlit as st
st.write("App file loaded")

import os
from datetime import datetime, timezone
import pandas as pd
import streamlit as st

# Google Sheets
import gspread
from google.oauth2.service_account import Credentials

# Fuzzy matching
try:
    from rapidfuzz import fuzz, process
    HAS_RAPIDFUZZ = True
except Exception:
    HAS_RAPIDFUZZ = False

st.set_page_config(page_title="BD Day – Contact Lockout", page_icon="📞", layout="wide")
st.title("📞 BD Day – Contact Lockout")
st.caption("Lock before you dial. Everyone sees locks instantly across brands. Fuzzy matching + domain + phone checks.")

# ----------------------------
# SETTINGS (SIDEBAR)
# ----------------------------
with st.sidebar:
    st.header("Settings")
    default_url = os.environ.get("SHEET_URL", st.secrets.get("SHEET_URL", "")) if hasattr(st, "secrets") else os.environ.get("SHEET_URL", "")
    sheet_url = st.text_input("Google Sheet URL", value=default_url, help="Share this sheet with the service account email below.")
    tz_name = st.selectbox("Timezone", ["Europe/London", "UTC"], index=0)
    fuzzy_threshold = st.slider("Fuzzy company match threshold", min_value=70, max_value=95, value=82, help="Higher = stricter matches")
    st.markdown("---")
    st.markdown("**Auth methods:**")
    st.markdown("• Local file: `service_account.json` in app folder")
    st.markdown("• Streamlit Cloud secrets: key `gcp_service_account` (JSON)")
    st.markdown("---")
    st.markdown("**Tip:** Exact matches are checked for email/phone. Domains are also matched (e.g. `@pwc.com`).")

# ----------------------------
# AUTH
# ----------------------------
def get_credentials():
    # Prefer Streamlit secrets (for Streamlit Cloud)
    if hasattr(st, "secrets") and "gcp_service_account" in st.secrets:
        service_account_info = dict(st.secrets["gcp_service_account"])
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = Credentials.from_service_account_info(service_account_info, scopes=scopes)
        return creds
    # Fallback to local file
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
        ws = sh.add_worksheet(title="Locks", rows=2000, cols=12)
        ws.update(
            "A1:I1",
            [["Timestamp","Date","Company","Contact Name","Email","Phone","Brand","Locked By","Notes"]],
        )
    return ws

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
    return " ".join(x.strip().lower().split())

def normalize_phone(p: str) -> str:
    if not p:
        return ""
    return "".join(ch for ch in p if ch.isdigit())

def email_domain(email: str) -> str:
    if not email:
        return ""
    e = email.strip().lower()
    if "@" in e:
        return e.split("@", 1)[1]
    return ""

# ----------------------------
# LOAD
# ----------------------------
if not sheet_url:
    st.warning("Add your Google Sheet URL in the sidebar to continue.")
    st.stop()

try:
    ws = open_sheet(sheet_url)
except Exception as e:
    st.error(f"Could not open sheet: {e}")
    st.stop()

rows = ws.get_all_records()
df = pd.DataFrame(rows, columns=["Timestamp","Date","Company","Contact Name","Email","Phone","Brand","Locked By","Notes"])

if not df.empty:
    df["Timestamp"] = pd.to_datetime(df["Timestamp"], errors="coerce")
    # Precompute normalized columns
    df["_company_n"] = df["Company"].astype(str).apply(normalize_text)
    df["_email_n"] = df["Email"].astype(str).apply(normalize_text)
    df["_domain"] = df["Email"].astype(str).apply(email_domain)
    df["_phone_n"] = df["Phone"].astype(str).apply(normalize_phone)
else:
    df = pd.DataFrame(columns=["Timestamp","Date","Company","Contact Name","Email","Phone","Brand","Locked By","Notes",
                               "_company_n","_email_n","_domain","_phone_n"])

# ----------------------------
# FORM
# ----------------------------
st.subheader("Lock a Contact")
with st.form("lock_form", clear_on_submit=True):
    col1, col2, col3 = st.columns([1.3,1,1])
    with col1:
        company = st.text_input("Company *")
        contact_name = st.text_input("Contact Name *")
        email = st.text_input("Email (recommended)")
        phone = st.text_input("Phone")
        notes = st.text_area("Notes (optional)", height=80)
    with col2:
        brand = st.selectbox("Your Brand *", ["Dartmouth Partners","Catalyst Partners","Pure Search","Other"])
        locked_by = st.text_input("Your Name *", value="")
        st.markdown(" ")
        st.markdown("**Duplicate Check (live)**")
    with col3:
        st.markdown("**Match Signals**")
        check_company = normalize_text(company)
        check_email = normalize_text(email)
        check_domain = email_domain(email)
        check_phone = normalize_phone(phone)

        hits = []
        if not df.empty:
            # Exact email
            if check_email:
                exact_email = df[df["_email_n"] == check_email]
                if not exact_email.empty:
                    hits.append(("Exact email", exact_email))

            # Exact phone
            if check_phone:
                exact_phone = df[df["_phone_n"] == check_phone]
                if not exact_phone.empty:
                    hits.append(("Exact phone", exact_phone))

            # Domain match
            if check_domain:
                dom = df[df["_domain"] == check_domain]
                if not dom.empty:
                    hits.append((f"Same email domain @{check_domain}", dom))

            # Fuzzy company (if rapidfuzz available)
            if check_company and HAS_RAPIDFUZZ:
                uniq_companies = df["_company_n"].dropna().unique().tolist()
                scored = []
                for comp in uniq_companies:
                    if not comp:
                        continue
                    score = fuzz.token_set_ratio(check_company, comp)
                    if score >= fuzzy_threshold:
                        scored.append((comp, score))
                if scored:
                    matched_vals = set(c for c, s in scored)
                    fuzzy_df = df[df["_company_n"].isin(matched_vals)]
                    if not fuzzy_df.empty:
                        hits.append((f"Fuzzy company ≥{fuzzy_threshold}", fuzzy_df))

        if hits:
            st.error("⚠️ Potential duplicate(s) found. Review below before locking.")
            for label, sub in hits:
                st.markdown(f"**{label}**")
                st.dataframe(
                    sub[["Timestamp","Company","Contact Name","Email","Phone","Brand","Locked By","Notes"]].sort_values("Timestamp", ascending=False),
                    use_container_width=True
                )
        else:
            st.success("✅ No duplicates found on company/email/phone/domain checks.")

    submitted = st.form_submit_button("🔒 Lock Contact")
    if submitted:
        if not company or not contact_name or not brand or not locked_by:
            st.warning("Please fill in all *required* fields.")
        else:
            ts = now_in_tz(tz_name)
            date_str = ts.strftime("%Y-%m-%d")
            ts_iso = ts.strftime("%Y-%m-%d %H:%M:%S")
            new_row = [ts_iso, date_str, company.strip(), contact_name.strip(), email.strip(), phone.strip(), brand, locked_by.strip(), notes.strip()]
            ws.append_row(new_row, value_input_option="USER_ENTERED")
            st.success("Contact locked for today. Visible to all teams now.")
            st.experimental_rerun()

# ----------------------------
# TODAY VIEW
# ----------------------------
st.subheader("Today’s Locks (Live)")
with st.expander("Filters", expanded=True):
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        q_company = st.text_input("Filter by Company")
    with c2:
        q_email = st.text_input("Filter by Email")
    with c3:
        brand_filter = st.multiselect("Filter by Brand", ["Dartmouth Partners","Catalyst Partners","Pure Search","Other"])
    with c4:
        q_phone = st.text_input("Filter by Phone (digits only)")

if not df.empty:
    today_str = now_in_tz(tz_name).strftime("%Y-%m-%d")
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

    # Mark dupes within today (any same email OR same phone OR same company norm)
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
