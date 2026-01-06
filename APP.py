import streamlit as st
import google.generativeai as genai
import gspread
from google.oauth2.service_account import Credentials
import json
import re

# ==========================================
# 1. 設定・認証エリア（診断モード）
# ==========================================

st.title("お支払いのご相談窓口")

# 進行状況を表示するエリア（デバッグ用）
status_area = st.empty()

def load_creds_and_connect():
    try:
        # 1. APIキーの読み込み
        gemini_key = st.secrets.get("GEMINI_API_KEY")
        sheet_key = st.secrets.get("SPREADSHEET_KEY")
        if not gemini_key or not sheet_key:
            st.error("エラー: APIキーまたはシートキーがSecretsに設定されていません。")
            st.stop()
        
        genai.configure(api_key=gemini_key)

        # 2. JSONキーの読み込み
        json_str = st.secrets.get("GCP_JSON_KEY")
        if not json_str:
            st.error("エラー: Secretsに 'GCP_JSON_KEY' が見つかりません。")
            st.stop()
        
        # 3. JSONパース（辞書化）
        try:
            # 前後の空白を除去して読み込み
            service_account_info = json.loads(json_str.strip())
        except json.JSONDecodeError as e:
            st.error(f"エラー: JSONデータの形式が正しくありません。\n詳細: {e}")
            st.stop()

        # 4. 秘密鍵の修正
        if "private_key" in service_account_info:
            # \n を改行コードに変換
            service_account_info["private_key"] = service_account_info["private_key"].replace("\\n", "\n")
        
        # 5. 認証オブジェクトの作成
        scopes = [
            'https://www.googleapis.com/auth/spreadsheets',
            'https://www.googleapis.com/auth/drive'
        ]
        creds = Credentials.from_service_account_info(service_account_info, scopes=scopes)
        
        # 6. gspreadに接続
        client = gspread.authorize(creds)
        
        # 7. シートを開く
        sheet = client.open_by_key(sheet_key).sheet1
        
        return sheet

    except Exception as e:
        st.error(f"システム接続エラーが発生しました。\n詳細: {e}")
        # エラーの詳細をログにも出す
        print(f"DEBUG ERROR: {e}")
        st.stop()

# データベース接続実行
sheet = load_creds_and_connect()

# ==========================================
# 2. アプリ本体ロジック
# ==========================================

# URLパラメータからIDを取得
query_params = st.query_params
user_id_str = query_params.get("id", None)

if not user_id_str:
    st.error("アクセス用のIDが指定されていません。URLをご確認ください。")
    st.stop()

# ユーザー検索
customer = None
row_index = -1

try:
    records = sheet.get_all_records()
    for i, item in enumerate(records):
        # 文字列として比較
        if str(item.get("Camel企業id")) == str(user_id_str):
            customer = item
            row_index = i + 2
            break
except Exception as e:
    st.error(f"データ取得エラー: {e}")
    st.stop()

if not customer:
    st.error("お客様情報が見つかりませんでした。")
    st.stop()

# 顧客情報の取得
existing_email_addr = customer.get('送付先メアド', '登録なし')
company_name = customer.get('会社名', 'お客様')

# 金額の整形
raw_amount = customer.get('未入金額', 0)
try:
    amount_val = int(str(raw_amount).replace(",", ""))
    unpaid_amount = "{:,}".format(amount_val)
except:
    unpaid_amount = str(raw_amount)

# --- チャット処理 ---
if "messages" not in st.session_state:
    welcome_msg = (
        f"{company_name} 様、いつもご利用ありがとうございます。\n"
        f"現在、未入金額 {unpaid_amount}円 について確認のご連絡です。\n"
        "今後のご連絡は「メール」でのやり取りをご希望でしょうか？"
    )
    st.session_state.messages = [{"role": "assistant", "content": welcome_msg}]

for msg in st.session_state.messages:
    role_display = "user" if msg["role"] == "user" else "assistant"
    with st.chat_message(role_display):
        st.write(msg["content"])

if user_input := st.chat_input("ここに入力してください..."):
    with st.chat_message("user"):
        st.write(user_input)
    st.session_state.messages.append({"role": "user", "content": user_input})

    # Gemini設定
    model_name = "gemini-1.5-flash"
    system_instruction = f"""
    あなたは債権回収窓口の自動ボットです。相手は {company_name} 様です。
    相手の現在の登録メールアドレスは「{existing_email_addr}」です。
    
    親切かつ丁寧なビジネス口調で対応してください。
    メール連絡を希望された場合は「{existing_email_addr}」でよいか確認し、
    OKなら `[EMAIL_RECEIVED:{existing_email_addr}]` を、
    変更なら新しいアドレスを聞いて `[EMAIL_RECEIVED:新アドレス]` を出力してください。
    入金日が決まったら `[PROMISE_FIXED]` を出力してください。
    """

    gemini_history = []
    for m in st.session_state.messages:
        role = "user" if m["role"] == "user" else "model"
        gemini_history.append({"role": role, "parts": [m["content"]]})

    try:
        model = genai.GenerativeModel(
            model_name=model_name,
            system_instruction=system_instruction
        )
        
        chat = model.start_chat(history=gemini_history[:-1])
        response = chat.send_message(user_input)
        ai_msg = response.text
        
        display_msg = re.sub(r"\[.*?\]", "", ai_msg).strip()
        
        with st.chat_message("assistant"):
            st.write(display_msg)
        st.session_state.messages.append({"role": "assistant", "content": display_msg})

        # --- スプレッドシート更新 ---
        if "[EMAIL_RECEIVED:" in ai_msg:
            match = re.search(r"\[EMAIL_RECEIVED:(.*?)\]", ai_msg)
            if match:
                confirmed_email = match.group(1).strip()
                if confirmed_email != str(existing_email_addr).strip():
                    # G列更新
                    sheet.update_cell(row_index, 7, confirmed_email)
                # F列更新
                sheet.update_cell(row_index, 6, "メール対応中")

        elif "[PROMISE_FIXED]" in ai_msg:
            sheet.update_cell(row_index, 6, "入金約束済")

    except Exception as e:
        st.error(f"AI応答エラー: {e}")
