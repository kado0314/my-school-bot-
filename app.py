import os
import io
from flask import Flask, render_template, request, jsonify
import google.generativeai as genai
from PyPDF2 import PdfReader
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

app = Flask(__name__)

# --- 設定 ---
# Renderの環境変数からAPIキーを取得
GENAI_API_KEY = os.environ.get("GEMINI_API_KEY")
genai.configure(api_key=GENAI_API_KEY)

# Google Driveの設定
# サービスアカウントキーのパス（Renderではシークレットファイルとして保存します）
SERVICE_ACCOUNT_FILE = '/etc/secrets/credentials.json'
# ★ここにGoogleドライブのフォルダIDを貼り付けてください
DRIVE_FOLDER_ID = 'あなたのGoogleドライブフォルダIDをここに貼る' 

# 推奨モデル (Gemini 2.0 Flash)
model = genai.GenerativeModel('models/gemini-2.0-flash')

def get_drive_service():
    """Google Drive APIのサービスを取得"""
    # ローカル開発用（ファイルが同じ場所にある場合）とRender用でパスを調整
    creds_path = SERVICE_ACCOUNT_FILE
    if not os.path.exists(creds_path):
        # ローカルでテストする場合用（プロジェクト直下のcredentials.jsonを見る）
        creds_path = 'credentials.json'
        
    if not os.path.exists(creds_path):
        print("Warning: credentials.jsonが見つかりません。")
        return None

    scopes = ['https://www.googleapis.com/auth/drive.readonly']
    creds = service_account.Credentials.from_service_account_file(creds_path, scopes=scopes)
    return build('drive', 'v3', credentials=creds)

def load_pdfs_from_drive():
    """指定フォルダ内のPDFをダウンロードしてテキスト化する"""
    service = get_drive_service()
    if not service:
        return "認証ファイルが見つからないため、資料を読み込めませんでした。", []

    text_content = ""
    file_names = []

    try:
        # フォルダ内のPDFファイルを検索
        query = f"'{DRIVE_FOLDER_ID}' in parents and mimeType='application/pdf' and trashed=false"
        results = service.files().list(q=query, fields="files(id, name)").execute()
        items = results.get('files', [])

        if not items:
            return "フォルダ内にPDFファイルが見つかりませんでした。", []

        for item in items:
            print(f"Loading: {item['name']}...")
            file_id = item['id']
            file_names.append(item['name'])

            # メモリ上にファイルをダウンロード
            request = service.files().get_media(fileId=file_id)
            file_stream = io.BytesIO()
            downloader = MediaIoBaseDownload(file_stream, request)
            done = False
            while done is False:
                status, done = downloader.next_chunk()

            # PDFからテキスト抽出
            file_stream.seek(0)
            reader = PdfReader(file_stream)
            text_content += f"\n--- File: {item['name']} Start ---\n"
            for page in reader.pages:
                extracted = page.extract_text()
                if extracted:
                    text_content += extracted + "\n"
            text_content += f"--- File: {item['name']} End ---\n"

    except Exception as e:
        print(f"Drive API Error: {e}")
        return f"エラーが発生しました: {e}", []

    return text_content, file_names

# アプリ起動時に一度だけDriveからデータを読み込む
# ※Googleドライブに新しいファイルを追加したら、Renderで再デプロイ（Restart）すると反映されます
print("Starting to load PDFs from Drive...")
SYSTEM_CONTEXT, LOADED_FILES = load_pdfs_from_drive()
print("Loading complete.")

@app.route('/')
def index():
    return render_template('index.html', files=LOADED_FILES)

@app.route('/chat', methods=['POST'])
def chat():
    user_message = request.json.get('message')
    if not user_message:
        return jsonify({'error': 'No message provided'}), 400

    # プロンプト作成
    prompt = f"""
    あなたは学校のホームページに設置された親切な質問応答ボットです。
    以下の「Googleドライブから読み込んだ資料」に基づいて、ユーザーの質問に答えてください。
    資料にない情報は「申し訳ありません、その情報は手元の資料に含まれていません」と答えてください。

    [参照資料]
    {SYSTEM_CONTEXT}

    [ユーザーの質問]
    {user_message}
    """

    try:
        response = model.generate_content(prompt)
        return jsonify({'reply': response.text})
    except Exception as e:
        return jsonify({'reply': 'エラーが発生しました。'}), 500

if __name__ == '__main__':
    app.run(debug=True)
