import streamlit as st
import google.generativeai as genai
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import re

# ==========================================
# 1. 設定・認証エリア (Secretsから読み込み)
# ==========================================

# ローカル開発時は .streamlit/secrets.toml を参照
# Streamlit Cloud時は管理画面の Secrets に設定
try:
    GEMINI_API_KEY = st.secrets["GEMINI_API_KEY"]
    # サービスアカウント情報はJSONの中身をそのままSecretsに登録して辞書として読み込む
    GCP_SERVICE_ACCOUNT = st.secrets["gcp_service_account"]
except FileNotFoundError:
    st.error("Secretsファイルが見つかりません。")
    st.stop()

genai.configure(api_key=GEMINI_API_KEY)
model_name = "gemini-1.5-flash"

# Googleスプレッドシート認証設定
SCOPE = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
SPREADSHEET_KEY = st.secrets["SPREADSHEET_KEY"] # シートIDも隠すのがベスト

# 列の定義 (実際のシートに合わせて調整してください)
# ※Pythonは0始まり、Gspreadのcol指定は1始まりに注意
COL_ID = 1          # A列
COL_NAME = 2        # B列
COL_EMAIL_EXISTING = 3 # C列
COL_AMOUNT = 4      # D列
COL_BOT_URL = 5     # E列
COL_STATUS = 6      # F列
COL_EMAIL_NEW = 7   # G列

def get_database():
    # from_json_keyfile_name ではなく from_json_keyfile_dict を使用
    creds = ServiceAccountCredentials.from_json_keyfile_dict(GCP_SERVICE_ACCOUNT, SCOPE)
    client_gs = gspread.authorize(creds)
    sheet = client_gs.open_by_key(SPREADSHEET_KEY).sheet1
    return sheet

# ==========================================
# 2. アプリ本体
# ==========================================

st.title("お支払いのご相談窓口")

# URLパラメータからIDを取得
query_params = st.query_params
user_id_str = query_params.get("id", None) # ★できれば "key" など推測不可なものに変更推奨

if not user_id_str:
    st.error("アクセス用のIDが指定されていません。")
    st.stop()

# DB(スプレッドシート)からユーザー検索
customer = None
row_index = -1

try:
    sheet = get_database()
    records = sheet.get_all_records()
    
    # Camel企業id は数値の場合と文字列の場合があるため str() で統一比較
    for i, item in enumerate(records):
        # get_all_records はヘッダー行を除くため、実際の行番号は i + 2
        if str(item.get("Camel企業id")) == user_id_str:
            customer = item
            row_index = i + 2
            break
        
except Exception as e:
    st.error(f"データベース接続エラー: {e}")
    st.stop()

if not customer:
    st.error("お客様情報が見つかりませんでした。URLをご確認ください。")
    st.stop()

# 登録済みのメアド
existing_email_addr = customer.get('送付先メアド', '登録なし')
company_name = customer.get('会社名', 'お客様')
unpaid_amount = customer.get('未入金額', 0)

# --- チャットの初期化 ---
if "messages" not in st.session_state:
    welcome_msg = (
        f"{company_name} 様、いつもご利用ありがとうございます。\n"
        f"現在、未入金額 {unpaid_amount}円 について確認のご連絡です。\n"
        "今後のご連絡は「メール」でのやり取りをご希望でしょうか？"
    )
    st.session_state.messages = [{"role": "assistant", "content": welcome_msg}]

# チャット履歴表示
for msg in st.session_state.messages:
    role_display = "user" if msg["role"] == "user" else "assistant"
    with st.chat_message(role_display):
        st.write(msg["content"])

# --- ユーザー入力処理 ---
if user_input := st.chat_input("ここに入力してください..."):
    with st.chat_message("user"):
        st.write(user_input)
    st.session_state.messages.append({"role": "user", "content": user_input})

    # システムプロンプト
    system_instruction = f"""
    あなたは債権回収窓口の自動ボットです。相手は {company_name} 様です。
    相手の現在の登録メールアドレスは「{existing_email_addr}」です。
    
    【基本ルール】
    親切かつ丁寧なビジネス口調で対応してください。
    
    【メール連絡のフロー】
    1. ユーザーが「メール連絡を希望」した場合:
       「現在ご登録のメールアドレス（{existing_email_addr}）への送付でよろしいでしょうか？」と確認してください。
    
    2. ユーザーが「はい」「それでいい」と答えた場合:
       「承知いたしました。担当に伝達のうえ、3営業日以内に回答します。」と答え、
       出力の最後に `[EMAIL_RECEIVED:{existing_email_addr}]` をつけてください。
       
    3. ユーザーが「いいえ」「違う」と答えた場合:
       「恐れ入りますが、ご希望のメールアドレスを教えていただけますか？」と聞いてください。
    
    4. ユーザーが「新しいメールアドレス」を入力した場合:
       「承知いたしました。担当に伝達のうえ、3営業日以内に回答します。」と答え、
       出力の最後に `[EMAIL_RECEIVED:入力されたアドレス]` をつけてください。

    【入金約束のフロー】
    会話の中で入金日が確定したら `[PROMISE_FIXED]` をつけてください。
    """

    # --- Gemini用に履歴データを変換 ---
    # Gemini APIのエラー回避: historyの先頭はUserである必要があるケースが多い、
    # またはroleの順番を守る必要があるため、単純変換のみ行う。
    gemini_history = []
    for m in st.session_state.messages:
        role = "user" if m["role"] == "user" else "model"
        gemini_history.append({"role": role, "parts": [m["content"]]})

    try:
        model = genai.GenerativeModel(
            model_name=model_name,
            system_instruction=system_instruction
        )
        
        # 直前のメッセージを除いたものを履歴とし、直前のメッセージをsend_messageに渡す
        # ただし、初回（履歴がassistantのみ）の場合、historyに入れると「Userから始まっていない」エラーになることがあるため調整
        
        history_for_api = gemini_history[:-1]
        
        # history_for_api の先頭が model の場合、Geminiがエラーを吐くことがあるため
        # 必要であればダミーのUserメッセージを入れる等の対策が必要ですが、
        # 1.5 Flashは比較的寛容なのでこのまま試行します。
        
        chat = model.start_chat(history=history_for_api)
        response = chat.send_message(user_input)
        
        ai_msg = response.text
        
        # タグの除去と表示
        display_msg = re.sub(r"\[.*?\]", "", ai_msg).strip()
        
        with st.chat_message("assistant"):
            st.write(display_msg)
        st.session_state.messages.append({"role": "assistant", "content": display_msg})

        # --- 裏側の処理 (スプレッドシート更新) ---
        if "[EMAIL_RECEIVED:" in ai_msg:
            match = re.search(r"\[EMAIL_RECEIVED:(.*?)\]", ai_msg)
            if match:
                confirmed_email = match.group(1).strip()
                # 既存メアドと比較して異なれば書き込み
                if confirmed_email != str(existing_email_addr).strip():
                    sheet.update_cell(row_index, COL_EMAIL_NEW, confirmed_email)
                
                sheet.update_cell(row_index, COL_STATUS, "メール対応中")

        elif "[PROMISE_FIXED]" in ai_msg:
            sheet.update_cell(row_index, COL_STATUS, "入金約束済")

    except Exception as e:
        st.error(f"AI応答エラー: {e}")
