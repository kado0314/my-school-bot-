import os
import io
import datetime
from flask import Flask, render_template, request, jsonify
import google.generativeai as genai
from PyPDF2 import PdfReader
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import gspread

app = Flask(__name__)

# --- 設定エリア ---
GENAI_API_KEY = os.environ.get("GEMINI_API_KEY")
genai.configure(api_key=GENAI_API_KEY)

SERVICE_ACCOUNT_FILE = '/etc/secrets/credentials.json'

# ★ここにGoogleドライブのフォルダIDを貼り付けてください
DRIVE_FOLDER_ID = '1fJ3Mbrcw-joAsX33aBu0z4oSQu7I0PhP' 

# ★ここにスプレッドシートIDを貼り付けてください
SPREADSHEET_ID = '1NK0ixXY9hOWuMib22wZxmFX6apUV7EhTDawTXPganZg'

# モデル設定 (Gemini 2.0 Flash)
model = genai.GenerativeModel('models/gemini-2.0-flash')

# グローバル変数として定義（更新機能で書き換えるため）
SYSTEM_CONTEXT = ""
LOADED_FILES = []

def get_credentials():
    """認証情報を取得（ドライブとスプレッドシート両用）"""
    creds_path = SERVICE_ACCOUNT_FILE
    if not os.path.exists(creds_path):
        creds_path = 'credentials.json'
        
    if not os.path.exists(creds_path):
        print("Warning: credentials.json not found.")
        return None

    scopes = [
        'https://www.googleapis.com/auth/drive.readonly',
        'https://www.googleapis.com/auth/spreadsheets' 
    ]
    return service_account.Credentials.from_service_account_file(creds_path, scopes=scopes)

def load_pdfs_from_drive():
    """Google DriveからPDFを読み込む"""
    creds = get_credentials()
    if not creds:
        return "認証エラー", []
    
    service = build('drive', 'v3', credentials=creds)
    text_content = ""
    file_list_data = [] # 名前とURLを入れるリスト

    try:
        # webViewLink (閲覧用URL) も取得
        query = f"'{DRIVE_FOLDER_ID}' in parents and mimeType='application/pdf' and trashed=false"
        results = service.files().list(q=query, fields="files(id, name, webViewLink)").execute()
        items = results.get('files', [])

        if not items:
            return "フォルダ内にPDFファイルが見つかりませんでした。", []

        for item in items:
            print(f"Loading: {item['name']}...")
            
            # 名前とURLを保存
            file_list_data.append({
                'name': item['name'],
                'url': item.get('webViewLink', '#') 
            })

            # PDFの中身を読む処理
            request = service.files().get_media(fileId=item['id'])
            file_stream = io.BytesIO()
            downloader = MediaIoBaseDownload(file_stream, request)
            done = False
            while done is False:
                _, done = downloader.next_chunk()

            file_stream.seek(0)
            reader = PdfReader(file_stream)
            text_content += f"\n--- File: {item['name']} ---\n"
            for page in reader.pages:
                extracted = page.extract_text()
                if extracted:
                    text_content += extracted + "\n"

    except Exception as e:
        print(f"Drive Error: {e}")
        return f"エラーが発生しました: {e}", []

    return text_content, file_list_data

def save_log_to_sheet(user_msg, bot_msg):
    """スプレッドシートにログを保存する"""
    try:
        creds = get_credentials()
        if not creds:
            return

        client = gspread.authorize(creds)
        sheet = client.open_by_key(SPREADSHEET_ID).sheet1
        now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        try:
            sheet.append_row([now, user_msg, bot_msg])
            print("Log saved to sheet.")
        except Exception as sheet_error:
            print(f"Sheet Append Error: {sheet_error}")

    except Exception as e:
        print(f"Logging Error: {e}")

# --- 起動時の処理 ---
print("Starting to load PDFs from Drive...")
SYSTEM_CONTEXT, LOADED_FILES = load_pdfs_from_drive()
print("Loading complete.")


# --- ルーティング ---

@app.route('/')
def index():
    return render_template('index.html', files=LOADED_FILES)

@app.route('/refresh')
def refresh_data():
    """
    【追加機能】このURLにアクセスすると、強制的にGoogleドライブを読み直します
    """
    global SYSTEM_CONTEXT, LOADED_FILES
    
    print("Reloading data from Drive...")
    new_context, new_files = load_pdfs_from_drive()
    
    if new_files:
        SYSTEM_CONTEXT = new_context
        LOADED_FILES = new_files
        return jsonify({
            'status': 'success', 
            'message': '知識データを更新しました！', 
            'files': new_files
        })
    else:
        return jsonify({
            'status': 'error', 
            'message': '更新に失敗しました。PDFが見つからないかエラーが発生しました。'
        })

@app.route('/chat', methods=['POST'])
def chat():
    data = request.json
    user_message = data.get('message')
    history_list = data.get('history', [])
    
    if not user_message:
        return jsonify({'error': 'No message provided'}), 400

    # 履歴をテキスト化
    history_text = ""
    for chat in history_list[-6:]:
        role = "ユーザー" if chat['role'] == 'user' else "AI"
        content = chat['text']
        history_text += f"{role}: {content}\n"

    prompt = f"""
    あなたは厳格な事実確認を行う学校の質問応答システムです。
    
    【重要ルール】
    1. 以下の[参照資料]に書かれている内容**のみ**を根拠として回答してください。 
    2. [これまでの会話]の流れを考慮して回答してください（文脈を理解してください）。 
    3. あなた自身の知識や推測、一般論を混ぜるときはわかりやい形で、知識、推論と答えてください。 
    4. 資料に答えが見つからない場合は、「申し訳ありません、資料にはその情報がありません」と答えてください。 
    5. 参照して答えた資料の名前とページ数も出力してください。

    [参照資料]
    {SYSTEM_CONTEXT}

    [これまでの会話]
    {history_text}

    [今回のユーザーの質問]
    {user_message}
    """

    try:
        response = model.generate_content(prompt)
        bot_reply = response.text
        
        save_log_to_sheet(user_message, bot_reply)
        
        return jsonify({'reply': bot_reply})
    except Exception as e:
        print(f"Gemini Error: {e}")
        if "429" in str(e):
            return jsonify({'reply': '申し訳ありません。現在アクセスが集中しており、一時的に利用できません。少し待ってから再度お試しください。'})
        return jsonify({'reply': 'エラーが発生しました。'}), 500

if __name__ == '__main__':
    app.run(debug=True)
