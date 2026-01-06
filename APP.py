import streamlit as st
import google.generativeai as genai
import gspread
import json
import tempfile
import os
import re

# ==========================================
# 1. 設定・認証エリア
# ==========================================

try:
    GEMINI_API_KEY = st.secrets["GEMINI_API_KEY"]
    SPREADSHEET_KEY = st.secrets["SPREADSHEET_KEY"]
    
    # Secretsの中身を辞書として確保
    raw_key = st.secrets["gcp_service_account"]
    
    # 文字列なら辞書に変換、すでにならそのまま
    if isinstance(raw_key, str):
        try:
            service_account_info = json.loads(raw_key)
        except json.JSONDecodeError:
            service_account_info = dict(raw_key)
    else:
        service_account_info = dict(raw_key)

    # 秘密鍵の改行コード修正
    if "private_key" in service_account_info:
        pk = service_account_info["private_key"]
        service_account_info["private_key"] = pk.replace("\\n", "\n")

except Exception as e:
    st.error(f"認証情報の読み込みエラー: {e}")
    st.stop()

# Geminiの設定
genai.configure(api_key=GEMINI_API_KEY)
model_name = "gemini-1.5-flash"

# スプレッドシート権限設定
SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive'
]

def get_database():
    # ★ここが解決策：一時ファイルを作成して「ファイル」として読み込ませる
    # これにより "Cannot convert str..." エラーを物理的に回避します
    temp_filename = None
    try:
        # 1. 一時的なJSONファイルを作成
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump(service_account_info, f)
            temp_filename = f.name
        
        # 2. そのファイルを指定して認証（これが最も確実な方法）
        client_gs = gspread.service_account(filename=temp_filename, scopes=SCOPES)
        
        # 3. シートを開く
        sheet = client_gs.open_by_key(SPREADSHEET_KEY).sheet1
        return sheet

    except Exception as e:
        st.error(f"スプレッドシート接続エラー詳細: {e}")
        raise e
    finally:
        # 4. 後片付け（一時ファイルを削除）
        if temp_filename and os.path.exists(temp_filename):
            try:
                os.remove(temp_filename)
            except:
                pass

# ==========================================
# 2. アプリ本体
# ==========================================

st.title("お支払いのご相談窓口")

# URLパラメータからIDを取得
query_params = st.query_params
user_id_str = query_params.get("id", None)

if not user_id_str:
    st.error("アクセス用のIDが指定されていません。")
    st.stop()

# DB(スプレッドシート)からユーザー検索
customer = None
row_index = -1

try:
    sheet = get_database()
    records = sheet.get_all_records()
    
    # ユーザー検索
    for i, item in enumerate(records):
        if str(item.get("Camel企業id")) == str(user_id_str):
            customer = item
            row_index = i + 2
            break
        
except Exception as e:
    st.stop()

if not customer:
    st.error("お客様情報が見つかりませんでした。URLをご確認ください。")
    st.stop()

# 顧客情報の取得
existing_email_addr = customer.get('送付先メアド', '登録なし')
company_name = customer.get('会社名', 'お客様')

# 未入金額の整形
raw_amount = customer.get('未入金額', 0)
try:
    amount_val = int(str(raw_amount).replace(",", ""))
    unpaid_amount = "{:,}".format(amount_val)
except:
    unpaid_amount = str(raw_amount)

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

    # Gemini用履歴変換
    gemini_history = []
    for m in st.session_state.messages:
        role = "user" if m["role"] == "user" else "model"
        gemini_history.append({"role": role, "parts": [m["content"]]})

    try:
        model = genai.GenerativeModel(
            model_name=model_name,
            system_instruction=system_instruction
        )
        
        # 履歴と最新メッセージの分離
        history_for_api = gemini_history[:-1]
        
        chat = model.start_chat(history=history_for_api)
        response = chat.send_message(user_input)
        
        ai_msg = response.text
        
        # 表示用メッセージ（タグ除去）
        display_msg = re.sub(r"\[.*?\]", "", ai_msg).strip()
        
        with st.chat_message("assistant"):
            st.write(display_msg)
        st.session_state.messages.append({"role": "assistant", "content": display_msg})

        # --- スプレッドシート更新処理 ---
        if "[EMAIL_RECEIVED:" in ai_msg:
            match = re.search(r"\[EMAIL_RECEIVED:(.*?)\]", ai_msg)
            if match:
                confirmed_email = match.group(1).strip()
                if confirmed_email != str(existing_email_addr).strip():
                    sheet.update_cell(row_index, 7, confirmed_email)
                
                sheet.update_cell(row_index, 6, "メール対応中")

        elif "[PROMISE_FIXED]" in ai_msg:
            sheet.update_cell(row_index, 6, "入金約束済")

    except Exception as e:
        st.error(f"AI応答エラー: {e}")
