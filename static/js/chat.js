// Chat frontend — SSE streaming support
document.addEventListener("DOMContentLoaded", () => {
    const form = document.getElementById("chat-form");
    const input = document.getElementById("message-input");
    const messagesDiv = document.getElementById("messages");
    const sendBtn = form.querySelector("button");

    let sending = false;

    // Load existing messages on page load
    loadHistory();

    function addMessage(role, content) {
        const div = document.createElement("div");
        div.className = `message ${role}`;
        div.textContent = content;
        messagesDiv.appendChild(div);
        scrollToBottom();
        return div;
    }

    function createAssistantBubble() {
        const div = document.createElement("div");
        div.className = "message assistant";
        messagesDiv.appendChild(div);
        scrollToBottom();
        return div;
    }

    function scrollToBottom() {
        messagesDiv.scrollTop = messagesDiv.scrollHeight;
    }

    function setEnabled(enabled) {
        sending = !enabled;
        input.disabled = !enabled;
        sendBtn.disabled = !enabled;
    }

    async function loadHistory() {
        try {
            const res = await fetch("/api/messages?limit=50");
            if (res.status === 401) {
                window.location.href = "/login";
                return;
            }
            const messages = await res.json();
            for (const msg of messages) {
                if (msg.role === "user" || msg.role === "assistant") {
                    addMessage(msg.role, msg.content);
                }
            }
        } catch (err) {
            // Silent fail on history load — not critical
        }
    }

    form.addEventListener("submit", async (e) => {
        e.preventDefault();
        if (sending) return;

        const text = input.value.trim();
        if (!text) return;

        addMessage("user", text);
        input.value = "";
        setEnabled(false);

        const bubble = createAssistantBubble();

        try {
            const res = await fetch("/api/chat", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ message: text }),
            });

            if (res.status === 401) {
                window.location.href = "/login";
                return;
            }

            if (!res.ok) {
                bubble.textContent = `Error: ${res.status} ${res.statusText}`;
                bubble.className = "message error";
                setEnabled(true);
                return;
            }

            const reader = res.body.getReader();
            const decoder = new TextDecoder();
            let buffer = "";

            while (true) {
                const { done, value } = await reader.read();
                if (done) break;

                buffer += decoder.decode(value, { stream: true });

                // Process complete SSE lines
                const lines = buffer.split("\n");
                // Keep the last incomplete line in the buffer
                buffer = lines.pop();

                for (const line of lines) {
                    if (!line.startsWith("data: ")) continue;

                    const jsonStr = line.slice(6);
                    let event;
                    try {
                        event = JSON.parse(jsonStr);
                    } catch {
                        continue;
                    }

                    if (event.type === "token") {
                        bubble.textContent += event.text;
                        scrollToBottom();
                    } else if (event.type === "error") {
                        bubble.textContent += event.detail || "Unknown error";
                        bubble.className = "message error";
                    } else if (event.type === "done") {
                        // Stream complete
                    }
                }
            }
        } catch (err) {
            bubble.textContent = "Connection error: " + err.message;
            bubble.className = "message error";
        }

        setEnabled(true);
        input.focus();
    });
});
