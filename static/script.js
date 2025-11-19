async function sendMessage() {
    const inputField = document.getElementById('user-input');
    const message = inputField.value.trim();
    const chatContainer = document.getElementById('chat-container');

    if (!message) return;

    // ユーザーのメッセージを表示
    appendMessage('user', message);
    inputField.value = '';

    // ローディング表示（簡易的）
    const loadingId = 'loading-' + Date.now();
    const loadingHtml = `
        <div class="chat-message bot-message" id="${loadingId}">
            <img src="/static/bitboticon.png" class="bot-icon">
            <div class="message-content">考え中...</div>
        </div>
    `;
    chatContainer.insertAdjacentHTML('beforeend', loadingHtml);
    chatContainer.scrollTop = chatContainer.scrollHeight;

    try {
        const response = await fetch('/chat', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({ message: message }),
        });

        const data = await response.json();
        
        // ローディングを削除
        document.getElementById(loadingId).remove();

        // ボットの回答を表示
        appendMessage('bot', data.reply);

    } catch (error) {
        document.getElementById(loadingId).remove();
        appendMessage('bot', 'エラーが発生しました。もう一度お試しください。');
        console.error('Error:', error);
    }
}

function appendMessage(sender, text) {
    const chatContainer = document.getElementById('chat-container');
    const div = document.createElement('div');
    
    div.classList.add('chat-message');
    div.classList.add(sender === 'user' ? 'user-message' : 'bot-message');

    let contentHtml = '';
    
    if (sender === 'bot') {
        contentHtml += `<img src="/static/bitboticon.png" class="bot-icon">`;
    }
    
    // HTMLエスケープ処理（簡易）
    const safeText = text.replace(/\n/g, '<br>');
    
    contentHtml += `<div class="message-content">${safeText}</div>`;
    
    div.innerHTML = contentHtml;
    chatContainer.appendChild(div);
    chatContainer.scrollTop = chatContainer.scrollHeight;
}

// Enterキーで送信
document.getElementById('user-input').addEventListener('keypress', function (e) {
    if (e.key === 'Enter') {
        sendMessage();
    }
});
