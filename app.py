import os
import io
import datetime
import time
import tempfile
from flask import Flask, render_template, request, jsonify
import google.generativeai as genai
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

# モデル設定 (B案では画像認識に強い 1.5-flash を推奨します)
# ※2.0-flashでも動きますが、安定性重視なら1.5-flashが良いです
model = genai.GenerativeModel('models/gemini-1.5-flash')

# グローバル変数（アップロード済みのファイルオブジェクトを保存）
UPLOADED_FILES_CACHE = [] 
FILE_LIST_DATA = []

def get_credentials():
    """認証情報を取得"""
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

def load_and_upload_pdfs():
    """Google DriveからPDFをダウンロードし、Geminiへアップロードする"""
    global UPLOADED_FILES_CACHE, FILE_LIST_DATA
    
    creds = get_credentials()
    if not creds:
        return [], []
    
    service = build('drive', 'v3', credentials=creds)
    
    # リセット
    UPLOADED_FILES_CACHE = []
    FILE_LIST_DATA = []

    try:
        # DriveからPDF一覧を取得
        query = f"'{DRIVE_FOLDER_ID}' in parents and mimeType='application/pdf' and trashed=false"
        results = service.files().list(q=query, fields="files(id, name, webViewLink)").execute()
        items = results.get('files', [])

        if not items:
            print("No PDF files found.")
            return [], []

        for item in items:
            print(f"Processing: {item['name']}...")
            
            # フロント表示用のリストに追加
            FILE_LIST_DATA.append({
                'name': item['name'],
                'url': item.get('webViewLink', '#')
            })

            # 1. Driveから一時ファイルとしてダウンロード
            request = service.files().get_media(fileId=item['id'])
            
            # 一時ファイルを作成して保存
            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp_file:
                downloader = MediaIoBaseDownload(tmp_file, request)
                done = False
                while done is False:
                    _, done = downloader.next_chunk()
                tmp_path = tmp_file.name

            # 2. Geminiサーバーへアップロード (File API)
            # ※ここで「画像も読める形式」としてAIに渡されます
            try:
                print(f"Uploading to Gemini: {item['name']}")
                uploaded_file = genai.upload_file(path=tmp_path, display_name=item['name'])
                
                # アップロード完了まで少し待機（処理中の場合があるため）
                while uploaded_file.state.name == "PROCESSING":
                    time.sleep(1)
                    uploaded_file = genai.get_file(uploaded_file.name)

                # 準備完了したファイルをリストに追加
                UPLOADED_FILES_CACHE.append(uploaded_file)
                print(f"Upload Complete: {item['name']}")

            except Exception as upload_error:
                print(f"Upload Error for {item['name']}: {upload_error}")
            finally:
                # 一時ファイルは削除
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)

    except Exception as e:
        print(f"Drive/Upload Error: {e}")
        return [], []

    return UPLOADED_FILES_CACHE, FILE_LIST_DATA

def save_log_to_sheet(user_msg, bot_msg):
    """ログ保存"""
    try:
        creds = get_credentials()
        if not creds: return
        client = gspread.authorize(creds)
        sheet = client.open_by_key(SPREADSHEET_ID).sheet1
        now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        sheet.append_row([now, user_msg, bot_msg])
    except Exception as e:
        print(f"Logging Error: {e}")

# --- 起動時にファイルを準備 ---
print("System starting... uploading files to Gemini...")
load_and_upload_pdfs()
print("System Ready.")

# --- ルーティング ---

@app.route('/')
def index():
    return render_template('index.html', files=FILE_LIST_DATA)

@app.route('/refresh')
def refresh_data():
    """知識の更新（再アップロード）"""
    print("Refreshing data...")
    uploaded, file_list = load_and_upload_pdfs()
    
    if file_list:
        return jsonify({
            'status': 'success', 
            'message': '知識データを更新しました！(ファイル再アップロード完了)', 
            'files': file_list
        })
    else:
        return jsonify({
            'status': 'error', 
            'message': '更新に失敗しました。'
        })

@app.route('/chat', methods=['POST'])
def chat():
    data = request.json
    user_message = data.get('message')
    history_list = data.get('history', [])
    
    if not user_message:
        return jsonify({'error': 'No message provided'}), 400

    # 履歴のテキスト化
    history_text = ""
    for chat in history_list[-2:]: # B案は容量食うので直近2つくらい推奨
        role = "ユーザー" if chat['role'] == 'user' else "AI"
        content = chat['text']
        history_text += f"{role}: {content}\n"

    # プロンプト（指示書）
    system_instruction = """
    あなたは厳格な事実確認を行う学校の質問応答システムです。
    
    【重要ルール】
    1. 添付された資料(PDF)の内容**のみ**を根拠に回答してください。
    2. 資料内の「グラフ」「表」「地図」「写真」も読み取って回答に活用してください。
    import os
import io
import datetime
import time
import tempfile
from flask import Flask, render_template, request, jsonify
import google.generativeai as genai
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
SPREADSHEET_ID = 'あなたのスプレッドシートIDをここに貼る'

# モデル設定 (B案では画像認識に強い 1.5-flash を推奨します)
# ※2.0-flashでも動きますが、安定性重視なら1.5-flashが良いです
model = genai.GenerativeModel('models/gemini-1.5-flash')

# グローバル変数（アップロード済みのファイルオブジェクトを保存）
UPLOADED_FILES_CACHE = [] 
FILE_LIST_DATA = []

def get_credentials():
    """認証情報を取得"""
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

def load_and_upload_pdfs():
    """Google DriveからPDFをダウンロードし、Geminiへアップロードする"""
    global UPLOADED_FILES_CACHE, FILE_LIST_DATA
    
    creds = get_credentials()
    if not creds:
        return [], []
    
    service = build('drive', 'v3', credentials=creds)
    
    # リセット
    UPLOADED_FILES_CACHE = []
    FILE_LIST_DATA = []

    try:
        # DriveからPDF一覧を取得
        query = f"'{DRIVE_FOLDER_ID}' in parents and mimeType='application/pdf' and trashed=false"
        results = service.files().list(q=query, fields="files(id, name, webViewLink)").execute()
        items = results.get('files', [])

        if not items:
            print("No PDF files found.")
            return [], []

        for item in items:
            print(f"Processing: {item['name']}...")
            
            # フロント表示用のリストに追加
            FILE_LIST_DATA.append({
                'name': item['name'],
                'url': item.get('webViewLink', '#')
            })

            # 1. Driveから一時ファイルとしてダウンロード
            request = service.files().get_media(fileId=item['id'])
            
            # 一時ファイルを作成して保存
            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp_file:
                downloader = MediaIoBaseDownload(tmp_file, request)
                done = False
                while done is False:
                    _, done = downloader.next_chunk()
                tmp_path = tmp_file.name

            # 2. Geminiサーバーへアップロード (File API)
            # ※ここで「画像も読める形式」としてAIに渡されます
            try:
                print(f"Uploading to Gemini: {item['name']}")
                uploaded_file = genai.upload_file(path=tmp_path, display_name=item['name'])
                
                # アップロード完了まで少し待機（処理中の場合があるため）
                while uploaded_file.state.name == "PROCESSING":
                    time.sleep(1)
                    uploaded_file = genai.get_file(uploaded_file.name)

                # 準備完了したファイルをリストに追加
                UPLOADED_FILES_CACHE.append(uploaded_file)
                print(f"Upload Complete: {item['name']}")

            except Exception as upload_error:
                print(f"Upload Error for {item['name']}: {upload_error}")
            finally:
                # 一時ファイルは削除
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)

    except Exception as e:
        print(f"Drive/Upload Error: {e}")
        return [], []

    return UPLOADED_FILES_CACHE, FILE_LIST_DATA

def save_log_to_sheet(user_msg, bot_msg):
    """ログ保存"""
    try:
        creds = get_credentials()
        if not creds: return
        client = gspread.authorize(creds)
        sheet = client.open_by_key(SPREADSHEET_ID).sheet1
        now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        sheet.append_row([now, user_msg, bot_msg])
    except Exception as e:
        print(f"Logging Error: {e}")

# --- 起動時にファイルを準備 ---
print("System starting... uploading files to Gemini...")
load_and_upload_pdfs()
print("System Ready.")

# --- ルーティング ---

@app.route('/')
def index():
    return render_template('index.html', files=FILE_LIST_DATA)

@app.route('/refresh')
def refresh_data():
    """知識の更新（再アップロード）"""
    print("Refreshing data...")
    uploaded, file_list = load_and_upload_pdfs()
    
    if file_list:
        return jsonify({
            'status': 'success', 
            'message': '知識データを更新しました！(ファイル再アップロード完了)', 
            'files': file_list
        })
    else:
        return jsonify({
            'status': 'error', 
            'message': '更新に失敗しました。'
        })

@app.route('/chat', methods=['POST'])
def chat():
    data = request.json
    user_message = data.get('message')
    history_list = data.get('history', [])
    
    if not user_message:
        return jsonify({'error': 'No message provided'}), 400

    # 履歴のテキスト化
    history_text = ""
    for chat in history_list[-2:]: # B案は容量食うので直近2つくらい推奨
        role = "ユーザー" if chat['role'] == 'user' else "AI"
        content = chat['text']
        history_text += f"{role}: {content}\n"

    # プロンプト（指示書）
    system_instruction = """
    あなたは厳格な事実確認を行う学校の質問応答システムです。
    
    【重要ルール】
    1. 添付された資料(PDF)の内容*根拠に回答してください。
    2. 資料内の「グラフ」「表」「地図」「写真」も読み取って回答に活用してください。
    2. [これまでの会話]の流れを考慮して回答してください（文脈を理解してください）。 
    4. あなた自身の知識や推測、一般論を混ぜるときはわかりやい形で、「知識、推論である」旨を伝えて答えてください。 
    5. 資料に答えが見つからない場合は、「申し訳ありません、資料にはその情報がありません」と答えてください。 
    6. 文体は丁寧な「です・ます」調で。
    7. 参照して答えた資料の名前とページ数も出力してください。
    
    [これまでの会話]
    """ + history_text

    # AIに渡すデータ： [指示, ファイル1, ファイル2..., ユーザーの質問]
    request_content = [system_instruction]
    request_content.extend(UPLOADED_FILES_CACHE) # アップロードしたファイルを全部載せる
    request_content.append(f"\n[ユーザーの質問]\n{user_message}")

    try:
        # 生成実行
        response = model.generate_content(request_content)
        bot_reply = response.text
        
        save_log_to_sheet(user_message, bot_reply)
        
        return jsonify({'reply': bot_reply})
    except Exception as e:
        print(f"Gemini Error: {e}")
        if "429" in str(e):
            return jsonify({'reply': '申し訳ありません。アクセス集中（容量オーバー）のため一時的に利用できません。時間を置いてお試しください。'})
        return jsonify({'reply': 'エラーが発生しました。'}), 500

if __name__ == '__main__':
    # アップロード時間を考慮してタイムアウトを長めに設定して起動
    # (※Render上では gunicorn コマンドの設定が優先されます)
    app.run(debug=True)
    4. 文体は丁寧な「です・ます」調で。
    
    
    [これまでの会話]
    """ + history_text

    # AIに渡すデータ： [指示, ファイル1, ファイル2..., ユーザーの質問]
    request_content = [system_instruction]
    request_content.extend(UPLOADED_FILES_CACHE) # アップロードしたファイルを全部載せる
    request_content.append(f"\n[ユーザーの質問]\n{user_message}")

    try:
        # 生成実行
        response = model.generate_content(request_content)
        bot_reply = response.text
        
        save_log_to_sheet(user_message, bot_reply)
        
        return jsonify({'reply': bot_reply})
    except Exception as e:
        print(f"Gemini Error: {e}")
        if "429" in str(e):
            return jsonify({'reply': '申し訳ありません。アクセス集中（容量オーバー）のため一時的に利用できません。時間を置いてお試しください。'})
        return jsonify({'reply': 'エラーが発生しました。'}), 500

if __name__ == '__main__':
    # アップロード時間を考慮してタイムアウトを長めに設定して起動
    # (※Render上では gunicorn コマンドの設定が優先されます)
    app.run(debug=True)
