let qrPollInterval = null;
let loginAttemptId = null;

function hideError() {
  document.getElementById("error-msg").style.display = "none";
}

function showError(msg) {
  const el = document.getElementById("error-msg");
  el.innerText = msg;
  el.style.display = "block";
}

async function generateQR() {
  hideError();

  try {
    const res = await fetch("/api/auth/qr/start", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
      },
      body: JSON.stringify({}),
    });
    const data = await res.json();

    if (res.ok) {
      if (data.status === "authorized") {
        window.location.href = "/";
        return;
      }

      document.getElementById("qr-status-text").innerText =
        "Scan with your Telegram App";

      const qrContainer = document.getElementById("qrcode");
      qrContainer.innerHTML = "";
      new QRCode(qrContainer, {
        text: data.url,
        width: 200,
        height: 200,
      });

      loginAttemptId = data.attempt_id;
      startQRPoll();
    } else {
      showError(data.error || "QR Gen Failed");
      document.getElementById("qr-status-text").innerText =
        "Error generating QR";
    }
  } catch (e) {
    showError("Network Error");
    document.getElementById("qr-status-text").innerText = "Connection Error";
  }
}

function startQRPoll() {
  if (qrPollInterval) clearInterval(qrPollInterval);
  let polling = false;
  qrPollInterval = setInterval(async () => {
    if (polling) return;
    polling = true;
    try {
      const res = await fetch(
        `/api/auth/qr/status?attempt_id=${encodeURIComponent(loginAttemptId)}`,
      );
      const data = await res.json();

      if (!res.ok) {
        clearInterval(qrPollInterval);
        showError(data.error || "Login attempt failed");
        return;
      }

      if (data.status === "authorized") {
        clearInterval(qrPollInterval);
        window.location.href = "/";
      } else if (data.status === "needs_password") {
        clearInterval(qrPollInterval);
        goToPasswordStep();
      } else if (data.status === "expired") {
        clearInterval(qrPollInterval);
        showError("QR Code Expired. Reload page.");
      }
    } catch (e) {
      console.error("Poll error", e);
    } finally {
      polling = false;
    }
  }, 2000);
}

async function goToPasswordStep() {
  document
    .querySelectorAll(".step")
    .forEach((s) => s.classList.remove("active"));
  document.getElementById("password-step").classList.add("active");

  try {
    const res = await fetch(
      `/api/auth/hint?attempt_id=${encodeURIComponent(loginAttemptId)}`,
    );
    const data = await res.json();
    if (data.hint)
      document.getElementById("password-hint-text").innerText =
        "Hint: " + data.hint;
  } catch (e) {}
}

window.verifyPassword = async function () {
  const btn = document.querySelector("#password-step button");
  btn.disabled = true;
  btn.innerText = "Verifying...";
  hideError();

  const password = document.getElementById("password").value;

  try {
    const res = await fetch("/api/auth/2fa", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
      },
      body: JSON.stringify({ password, attempt_id: loginAttemptId }),
    });
    const data = await res.json();

    if (res.ok) {
      window.location.href = "/";
    } else {
      showError(data.error || "Invalid Password");
      btn.disabled = false;
      btn.innerText = "Verify Password";
    }
  } catch (e) {
    showError("Network Error");
    btn.disabled = false;
    btn.innerText = "Verify Password";
  }
};

const forgetAccount = document.getElementById("forget-account");
if (forgetAccount) {
  forgetAccount.addEventListener("click", async () => {
    if (
      !confirm(
        "Forget this saved account and its rules on this manager? This cannot be undone and does not revoke the Telegram session. Existing action and debug logs remain until their normal expiry.",
      )
    )
      return;
    forgetAccount.disabled = true;
    try {
      const response = await fetch("/api/settings/reset", {
        method: "POST",
        headers: { "X-Requested-With": "XMLHttpRequest" },
      });
      if (!response.ok) throw new Error("Reset failed");
      window.location.reload();
    } catch (_) {
      showError("Could not forget the saved account. Try again.");
      forgetAccount.disabled = false;
    }
  });
}

window.addEventListener("load", generateQR);
