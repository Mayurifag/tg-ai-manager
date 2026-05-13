let qrPollInterval = null;

function hideError() {
    document.getElementById('error-msg').style.display = 'none';
}

function showError(msg) {
    const el = document.getElementById('error-msg');
    el.innerText = msg;
    el.style.display = 'block';
}

async function generateQR() {
    hideError();

    try {
        const res = await fetch('/api/auth/qr/start', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({})
        });
        const data = await res.json();

        if (res.ok) {
            if (data.status === 'authorized') {
                window.location.href = '/';
                return;
            }

            document.getElementById('qr-status-text').innerText = 'Scan with your Telegram App';

            const qrContainer = document.getElementById('qrcode');
            qrContainer.innerHTML = '';
            new QRCode(qrContainer, {
                text: data.url,
                width: 200,
                height: 200
            });

            startQRPoll();
        } else {
            showError(data.error || 'QR Gen Failed');
            document.getElementById('qr-status-text').innerText = 'Error generating QR';
        }
    } catch (e) {
        showError('Network Error');
        document.getElementById('qr-status-text').innerText = 'Connection Error';
    }
}

function startQRPoll() {
    if (qrPollInterval) clearInterval(qrPollInterval);
    qrPollInterval = setInterval(async () => {
        try {
            const res = await fetch('/api/auth/qr/status');
            const data = await res.json();

            if (data.status === 'authorized') {
                clearInterval(qrPollInterval);
                window.location.href = '/';
            } else if (data.status === 'needs_password') {
                clearInterval(qrPollInterval);
                goToPasswordStep();
            } else if (data.status === 'expired') {
                clearInterval(qrPollInterval);
                showError('QR Code Expired. Reload page.');
            }
        } catch (e) {
            console.error('Poll error', e);
        }
    }, 2000);
}

async function goToPasswordStep() {
    document.querySelectorAll('.step').forEach(s => s.classList.remove('active'));
    document.getElementById('password-step').classList.add('active');

    try {
        const res = await fetch('/api/auth/hint');
        const data = await res.json();
        if (data.hint) document.getElementById('password-hint-text').innerText = 'Hint: ' + data.hint;
    } catch (e) { }
}

window.verifyPassword = async function () {
    const btn = document.querySelector('#password-step button');
    btn.disabled = true;
    btn.innerText = 'Verifying...';
    hideError();

    const password = document.getElementById('password').value;

    try {
        const res = await fetch('/api/auth/2fa', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ password })
        });
        const data = await res.json();

        if (res.ok) {
            window.location.href = '/';
        } else {
            showError(data.error || 'Invalid Password');
            btn.disabled = false;
            btn.innerText = 'Verify Password';
        }
    } catch (e) {
        showError('Network Error');
        btn.disabled = false;
        btn.innerText = 'Verify Password';
    }
};

window.addEventListener('load', generateQR);
