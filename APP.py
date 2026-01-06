import streamlit as st
import google.generativeai as genai
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import json
import re
import datetime

# ==========================================
# 1. 設定・認証エリア
# ==========================================

# ★修正点：タイトルを変更しました
st.title("お支払いの一次ご相談窓口")

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
        f"{company_name} さま\n\n"
        "いつもご利用ありがとうございます。  \n"
        "Camelのご請求に関する、未入金金額の確認窓口でございます。\n\n"
        f"現在、ご請求金額のうち、{unpaid_amount}円のご入金が確認できかねております。  \n"
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
    
    # --- 日付情報の計算 ---
    today = datetime.date.today()
    today_str = today.strftime("%Y年%m月%d日")
    weekday_str = ["月","火","水","木","金","土","日"][today.weekday()]
    
    if today.month == 12:
        next_month = today.replace(year=today.year + 1, month=1, day=1)
    else:
        next_month = today.replace(month=today.month + 1, day=1)
    end_of_month = next_month - datetime.timedelta(days=1)
    end_of_month_str = end_of_month.strftime("%Y年%m月%d日")

    # ★システムプロンプト：パターンBで勝手に進まないよう指示を強化
    system_instruction = f"""
    あなたは債権回収窓口の自動ボットです。相手は {company_name} 様です。
    相手の現在の登録メールアドレスは「{existing_email_addr}」です。
    
    【日付情報の基準】
    本日は {today_str} ({weekday_str}曜日) です。
    当月の末日は {end_of_month_str} です。
    「来週の月曜」「今週末」などの日付表現は、この基準日をもとに具体的な日付（YYYY年M月D日）に変換してください。

    【重要：表示フォーマットについて】
    Web画面で正しく改行表示させるため、改行する箇所には必ず「空白行（2回改行）」を入れてください。
    また、指定されたセリフ以外（不要な「。」や挨拶）を勝手に追加しないでください。
    
    【重要：ステップの厳守】
    一度の回答で複数のステップをまとめて進めないでください。必ず1つのステップだけを実行して、ユーザーの返信を待ってください。

    【対応フロー】
    ユーザーの回答に応じて、以下の3つのパターンのいずれかで対応してください。

    パターンA：入金予定日を回答してくれた場合
      ユーザー：「来週の月曜」「10/25です」など
      
      ★重要チェック: 回答された日付が「{end_of_month_str}」より後（来月以降など）の場合は、
      「申し訳ございません。当システムでは {end_of_month_str} までのお約束のみ承っております。
      今月中でのお支払いは可能でしょうか？」と断ってください。
      
      日付が {end_of_month_str} 以前であれば、以下のテンプレートで回答してください。
      
      【出力テンプレート】
      「承知いたしました。

      ＝＝＝＝＝＝＝＝＝＝

      ご入金予定日：[YYYY年M月D日]

      ＝＝＝＝＝＝＝＝＝＝

      上記日程にて、社内へ共有させていただきます。

      内容に変更があれば再度ご入力ください。
      なければ、そのまま画面を閉じて終了してください。」

      （出力の最後に `[PROMISE_FIXED]` および `[PAYMENT_DATE:YYYY年M月D日]` をつける）

    パターンB：確認したいことがある / 質問がある / わからない / 担当と話したい / 当月払えない場合
      
      ★重要：ユーザーが既に入力内で事情を説明していたとしても、いきなりステップ2に進まず、必ずステップ1の返答（ヒアリング）を行ってください。

      ステップ1: まず詳細を聞き出してください。
      【出力テンプレート】
      「恐れ入ります。

      詳細なご事情や、ご希望についてお聞かせいただけますでしょうか？」

      ステップ2（ユーザーが再度詳細を入力した後）: メアド確認をしてください。
      【出力テンプレート】
      「承知いたしました。
      ご回答内容は担当者に申し送りいたします。

      回答は担当よりメールにてご連絡させていただきますが、
      現在のメールアドレス（{existing_email_addr}）への送付でよろしいでしょうか？」
      
      ステップ3（アドレス確認後）: 最終確認を出してください。
      【出力テンプレート】
      「承知いたしました。

      ＝＝＝＝＝＝＝＝＝＝

      メールアドレス：[確認したメールアドレス]

      ご質問内容：[ヒアリングした内容]

      ＝＝＝＝＝＝＝＝＝＝

      上記内容を担当へ伝達のうえ、3営業日以内にご連絡いたします。

      内容に変更があれば再度ご入力ください。
      なければ、そのまま画面を閉じて終了してください。」

      （出力の最後に隠しタグ `[EMAIL_RECEIVED:確認したメールアドレス]` `[INQUIRY_CONTENT:ヒアリングした内容]` をつける）

    パターンC：肯定のみ（はい、大丈夫です）の場合
      ボット：「ありがとうございます。いつ頃のご入金予定になりますでしょうか？」
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
        
        # 画面表示用にタグを除去
        display_msg = re.sub(r"\[.*?\]", "", ai_msg).strip()
        
        with st.chat_message("assistant"):
            st.write(display_msg)
        st.session_state.messages.append({"role": "assistant", "content": display_msg})

        # --- スプレッドシート更新 ---
        
        # 1. 質問内容（I列転記）
        if "[INQUIRY_CONTENT:" in ai_msg:
            match_content = re.search(r"\[INQUIRY_CONTENT:(.*?)\]", ai_msg, re.DOTALL)
            if match_content:
                inquiry_text = match_content.group(1).strip()
                sheet.update_cell(row_index, 9, inquiry_text)

        # 2. メールアドレス（G列転記 & ステータス更新）
        if "[EMAIL_RECEIVED:" in ai_msg:
            match_email = re.search(r"\[EMAIL_RECEIVED:(.*?)\]", ai_msg)
            if match_email:
                confirmed_email = match_email.group(1).strip()
                sheet.update_cell(row_index, 7, confirmed_email)
                sheet.update_cell(row_index, 6, "メール対応中")

        # 3. 入金約束（日付をH列転記 & ステータス更新）
        if "[PAYMENT_DATE:" in ai_msg:
            match_date = re.search(r"\[PAYMENT_DATE:(.*?)\]", ai_msg)
            if match_date:
                payment_date = match_date.group(1).strip()
                sheet.update_cell(row_index, 8, payment_date)

        if "[PROMISE_FIXED]" in ai_msg:
            sheet.update_cell(row_index, 6, "入金約束済")

    except Exception as e:
        st.error(f"AIエラー: {e}")
