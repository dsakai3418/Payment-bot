import streamlit as st
import google.generativeai as genai
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import json
import re

# ==========================================
# 1. 設定・認証エリア
# ==========================================

st.title("お支払いのご相談窓口")

def connect_to_google_sheet():
    try:
        # --- APIキーとシートIDの読み込み ---
        gemini_key = st.secrets.get("GEMINI_API_KEY")
        sheet_key = st.secrets.get("SPREADSHEET_KEY")
        
        if not gemini_key or not sheet_key:
            st.error("エラー: Secretsの設定が不足しています。")
            st.stop()
            
        genai.configure(api_key=gemini_key)

        # --- JSONキーの読み込み ---
        json_str = st.secrets.get("GCP_JSON_KEY")
        if not json_str:
            st.error("エラー: Secretsに 'GCP_JSON_KEY' が見つかりません。")
            st.stop()

        try:
            creds_dict = json.loads(json_str.strip())
        except json.JSONDecodeError as e:
            st.error(f"SecretsのJSON形式が正しくありません。\n詳細: {e}")
            st.stop()

        if "private_key" in creds_dict:
            creds_dict["private_key"] = creds_dict["private_key"].replace("\\n", "\n")

        # --- 認証と接続 ---
        scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        client = gspread.authorize(creds)
        sheet = client.open_by_key(sheet_key).sheet1
        return sheet

    except Exception as e:
        st.error(f"システム接続エラー: {e}")
        st.stop()

# モデル自動検出関数
def get_valid_model_name():
    try:
        models = list(genai.list_models())
        chat_models = [m.name for m in models if 'generateContent' in m.supported_generation_methods]
        
        for name in chat_models:
            if "gemini-1.5-flash" in name: return name
        for name in chat_models:
            if "gemini-1.5-pro" in name: return name
        for name in chat_models:
            if "gemini-pro" in name: return name
            
        if chat_models: return chat_models[0]
        return "models/gemini-pro"
    except:
        return "gemini-pro"

# データベース接続を実行
sheet = connect_to_google_sheet()

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
        if str(item.get("Camel企業id")) == str(user_id_str):
            customer = item
            row_index = i + 2
            break
except Exception as e:
    st.error(f"データ取得中にエラーが発生しました: {e}")
    st.stop()

if not customer:
    st.error("お客様情報が見つかりませんでした。")
    st.stop()

# 顧客情報の取得
existing_email_addr = customer.get('送付先メアド', '登録なし')
company_name = customer.get('会社名', 'お客様')

raw_amount = customer.get('未入金額', 0)
try:
    amount_val = int(str(raw_amount).replace(",", ""))
    unpaid_amount = "{:,}".format(amount_val)
except:
    unpaid_amount = str(raw_amount)

# --- チャットのUI表示 ---
if "messages" not in st.session_state:
    welcome_msg = (
        f"{company_name} 様\n\n"
        "いつもご利用ありがとうございます。\n"
        "Camelのご請求に関する、未入金金額の確認窓口でございます。\n\n"
        f"現在、ご請求金額のうち、{unpaid_amount}円のご入金が確認できかねております。\n"
        "つきましては、ご入金予定日をお伺いしてもよろしいでしょうか？"
    )
    st.session_state.messages = [{"role": "assistant", "content": welcome_msg}]

for msg in st.session_state.messages:
    role_display = "user" if msg["role"] == "user" else "assistant"
    with st.chat_message(role_display):
        st.write(msg["content"])

# --- ユーザー入力とAI応答 ---
if user_input := st.chat_input("ここに入力してください..."):
    with st.chat_message("user"):
        st.write(user_input)
    st.session_state.messages.append({"role": "user", "content": user_input})

    valid_model_name = get_valid_model_name()
    
    # ★システムプロンプト：I列転記用タグと、指定の回答フォーマットを追加
    system_instruction = f"""
    あなたは債権回収窓口の自動ボットです。相手は {company_name} 様です。
    相手の現在の登録メールアドレスは「{existing_email_addr}」です。
    
    【基本ルール】
    親切かつ丁寧なビジネス口調で対応してください。
    あなたは最初の挨拶で「入金予定日」を質問済みです。

    【対応フロー】
    ユーザーの回答に応じて、以下の3つのパターンのいずれかで対応してください。

    パターンA：入金予定日を回答してくれた場合
      ユーザー：「来週の月曜」「10/25です」など
      ボット：「承知いたしました。〇月〇日ですね、ありがとうございます。」と確認し、
             出力の最後に `[PROMISE_FIXED]` をつけて終了。

    パターンB：確認したいことがある / 質問がある / わからない / 担当と話したい場合
      ユーザー：「何の請求？」「請求書がない」「内訳を知りたい」など
      
      ステップ1: まず「ご不明な点があるとのこと、失礼いたしました。どのような内容をご確認されたいでしょうか？」と内容を聞き出してください。
      
      ステップ2（ユーザーが詳細を入力した後）:
        「承知いたしました。ご質問内容は担当者に申し送りいたします。
         回答は担当よりメールにてご連絡させていただきますが、
         現在のメールアドレス（{existing_email_addr}）への送付でよろしいでしょうか？」と確認してください。
      
      ステップ3（アドレス確認後）:
        以下の形式で回答してください。
        
        「承知いたしました。
        [確認したメールアドレス] に
        「[ヒアリングした質問内容]」
        を担当へ伝達のうえ、3営業日以内にご連絡いたします。」

        そして、出力の最後に必ず以下の2つのタグをつけてください。
        1. `[EMAIL_RECEIVED:確認したメールアドレス]`
        2. `[INQUIRY_CONTENT:ヒアリングした質問内容]`
        ※ `[INQUIRY_CONTENT:...]` の中身はスプレッドシートに転記されるため、短く要約せず、ユーザーが言った内容を含めてください。

    パターンC：肯定のみ（はい、大丈夫です）の場合
      ユーザー：「はい」「わかった」
      ボット：「ありがとうございます。いつ頃のご入金予定になりますでしょうか？」と再度日付を聞いてください。
    """

    gemini_history = []
    for m in st.session_state.messages:
        role = "user" if m["role"] == "user" else "model"
        gemini_history.append({"role": role, "parts": [m["content"]]})

    try:
        model = genai.GenerativeModel(
            model_name=valid_model_name,
            system_instruction=system_instruction
        )
        
        chat = model.start_chat(history=gemini_history[:-1])
        response = chat.send_message(user_input)
        
        ai_msg = response.text
        
        # 画面表示用にタグを除去（INQUIRY_CONTENTタグも消す）
        display_msg = re.sub(r"\[.*?\]", "", ai_msg).strip()
        
        with st.chat_message("assistant"):
            st.write(display_msg)
        st.session_state.messages.append({"role": "assistant", "content": display_msg})

        # --- スプレッドシート更新 ---
        
        # 1. 質問内容（I列転記）
        if "[INQUIRY_CONTENT:" in ai_msg:
            # 改行が含まれる場合に対応するため DOTALL フラグを使用
            match_content = re.search(r"\[INQUIRY_CONTENT:(.*?)\]", ai_msg, re.DOTALL)
            if match_content:
                inquiry_text = match_content.group(1).strip()
                # I列 = 9列目
                sheet.update_cell(row_index, 9, inquiry_text)

        # 2. メールアドレス（G列転記 & ステータス更新）
        if "[EMAIL_RECEIVED:" in ai_msg:
            match_email = re.search(r"\[EMAIL_RECEIVED:(.*?)\]", ai_msg)
            if match_email:
                confirmed_email = match_email.group(1).strip()
                if confirmed_email != str(existing_email_addr).strip():
                    sheet.update_cell(row_index, 7, confirmed_email) # G列
                sheet.update_cell(row_index, 6, "メール対応中") # F列

        # 3. 入金約束（ステータス更新）
        elif "[PROMISE_FIXED]" in ai_msg:
            sheet.update_cell(row_index, 6, "入金約束済")

    except Exception as e:
        st.error(f"AIエラー: {e}")
