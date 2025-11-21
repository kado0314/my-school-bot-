import os
import io
import datetime
from flask import Flask, render_template, request, jsonify
import google.generativeai as genai
from PyPDF2 import PdfReader
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import gspread # ★追加

app = Flask(__name__)

# --- 設定 ---
GENAI_API_KEY = os.environ.get("GEMINI_API_KEY")
genai.configure(api_key=GENAI_API_KEY)

SERVICE_ACCOUNT_FILE = '/etc/secrets/credentials.json'

# ★あなたのGoogleドライブのフォルダID
DRIVE_FOLDER_ID = '1fJ3Mbrcw-joAsX33aBu0z4oSQu7I0PhP' 

# ★【重要】ここにステップ1でメモしたスプレッドシートIDを入れてください
SPREADSHEET_ID = 'あなたのスプレッドシートIDをここに貼る'

model = genai.GenerativeModel('gemini-1.5-flash')

def get_credentials():
    """認証情報を取得（ドライブとスプレッドシート両用）"""
    creds_path = SERVICE_ACCOUNT_FILE
    if not os.path.exists(creds_path):
        creds_path = 'credentials.json' # ローカル用
        
    if not os.path.exists(creds_path):
        print("Warning: credentials.json not found.")
        return None

    # 権限の範囲にSpreadsheetsを追加
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
    file_names = []

    try:
        query = f"'{DRIVE_FOLDER_ID}' in parents and mimeType='application/pdf' and trashed=false"
        results = service.files().list(q=query, fields="files(id, name)").execute()
        items = results.get('files', [])

        if not items:
            return "PDFなし", []

        for item in items:
            print(f"Loading: {item['name']}...")
            file_names.append(item['name'])
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
        return f"Error: {e}", []

    return text_content, file_names

def save_log_to_sheet(user_msg, bot_msg):
    """スプレッドシートにログを保存する"""
    try:
        creds = get_credentials()
        if not creds:
            return

        # gspreadでスプレッドシートに接続
        client = gspread.authorize(creds)
        sheet = client.open_by_key(SPREADSHEET_ID).sheet1 # 1枚目のシートを取得

        # 現在時刻
        now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        # 行を追加 [日時, ユーザーの質問, AIの回答]
        sheet.append_row([now, user_msg, bot_msg])
        print("Log saved to sheet.")

    except Exception as e:
        # ログ保存に失敗してもチャットは止めない
        print(f"Logging Error: {e}")

# 起動時に学習
print("Starting to load PDFs...")
SYSTEM_CONTEXT, LOADED_FILES = load_pdfs_from_drive()
print("Loading complete.")

@app.route('/')
def index():
    return render_template('index.html', files=LOADED_FILES)

@app.route('/chat', methods=['POST'])
def chat():
    user_message = request.json.get('message')
    if not user_message:
        return jsonify({'error': 'No message'}), 400

    prompt = f"""
    あなたは学校の質問応答ボットです。
    以下の資料に基づいて回答してください。
    
    [資料]
    {SYSTEM_CONTEXT}

    [質問]
    {user_message}
    """

    try:
        response = model.generate_content(prompt)
        bot_reply = response.text

        # ★ここでログ保存を実行（バックグラウンドでエラーになっても無視して回答を返す）
        save_log_to_sheet(user_message, bot_reply)

        return jsonify({'reply': bot_reply})
    except Exception as e:
        print(f"Chat Error: {e}")
        return jsonify({'reply': 'エラーが発生しました。'}), 500

if __name__ == '__main__':
    app.run(debug=True)
