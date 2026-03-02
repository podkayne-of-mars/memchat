// Chat frontend — SSE streaming support + image handling
document.addEventListener("DOMContentLoaded", () => {
    const form = document.getElementById("chat-form");
    const input = document.getElementById("message-input");
    const messagesDiv = document.getElementById("messages");
    const sendBtn = form.querySelector("button[type=submit]");
    const attachBtn = document.getElementById("attach-btn");
    const fileInput = document.getElementById("image-file-input");
    const imagePreview = document.getElementById("image-preview");
    const previewImg = document.getElementById("preview-img");
    const removeImageBtn = document.getElementById("remove-image");

    let sending = false;

    // Pending image state
    let pendingImageData = null;   // raw base64 (no data: prefix)
    let pendingImageType = null;   // e.g. "image/png"

    const ALLOWED_TYPES = new Set(["image/jpeg", "image/png", "image/gif", "image/webp"]);
    const MAX_SIZE = 10 * 1024 * 1024; // 10 MB

    // Load existing messages on page load
    loadHistory();

    // --- Image helpers ---

    function readImageFile(file) {
        if (!ALLOWED_TYPES.has(file.type)) {
            alert("Unsupported image type. Use JPEG, PNG, GIF, or WebP.");
            return;
        }
        if (file.size > MAX_SIZE) {
            alert("Image too large (max 10 MB).");
            return;
        }
        const reader = new FileReader();
        reader.onload = () => {
            const dataUrl = reader.result;          // "data:<type>;base64,<data>"
            const base64 = dataUrl.split(",")[1];
            pendingImageData = base64;
            pendingImageType = file.type;
            showPreview(dataUrl);
        };
        reader.readAsDataURL(file);
    }

    function showPreview(dataUrl) {
        previewImg.src = dataUrl;
        imagePreview.style.display = "flex";
    }

    function clearPreview() {
        pendingImageData = null;
        pendingImageType = null;
        previewImg.src = "";
        imagePreview.style.display = "none";
        fileInput.value = "";
    }

    // --- Image input handlers ---

    // Attach button → trigger file picker
    attachBtn.addEventListener("click", () => fileInput.click());

    // File picker change
    fileInput.addEventListener("change", () => {
        if (fileInput.files.length > 0) {
            readImageFile(fileInput.files[0]);
        }
    });

    // Remove button on preview
    removeImageBtn.addEventListener("click", clearPreview);

    // Paste handler — intercept image paste anywhere on the page
    document.addEventListener("paste", (e) => {
        const items = e.clipboardData && e.clipboardData.items;
        if (!items) return;
        for (const item of items) {
            if (item.type.startsWith("image/")) {
                e.preventDefault();
                readImageFile(item.getAsFile());
                return;
            }
        }
    });

    // Drag-and-drop on chat container
    const chatContainer = document.getElementById("chat-container");
    chatContainer.addEventListener("dragover", (e) => {
        e.preventDefault();
        e.dataTransfer.dropEffect = "copy";
    });
    chatContainer.addEventListener("drop", (e) => {
        e.preventDefault();
        const files = e.dataTransfer.files;
        if (files.length > 0 && files[0].type.startsWith("image/")) {
            readImageFile(files[0]);
        }
    });

    // --- Rendering helpers ---

    function renderMarkdownLinks(text) {
        // Convert [text](url) to clickable <a> tags, escaping everything else
        const parts = [];
        let last = 0;
        const re = /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g;
        let m;
        while ((m = re.exec(text)) !== null) {
            if (m.index > last) {
                const span = document.createElement("span");
                span.textContent = text.slice(last, m.index);
                parts.push(span);
            }
            const a = document.createElement("a");
            a.href = m[2];
            a.textContent = m[1];
            a.target = "_blank";
            a.rel = "noopener noreferrer";
            parts.push(a);
            last = m.index + m[0].length;
        }
        if (last < text.length) {
            const span = document.createElement("span");
            span.textContent = text.slice(last);
            parts.push(span);
        }
        return parts;
    }

    function addMessage(role, content, imageDataUrl) {
        const div = document.createElement("div");
        div.className = `message ${role}`;
        if (imageDataUrl) {
            const img = document.createElement("img");
            img.className = "message-image";
            img.src = imageDataUrl;
            div.appendChild(img);
        }
        for (const node of renderMarkdownLinks(content)) {
            div.appendChild(node);
        }
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
                    let imageDataUrl = null;
                    if (msg.image_data && msg.image_media_type) {
                        imageDataUrl = `data:${msg.image_media_type};base64,${msg.image_data}`;
                    }
                    addMessage(msg.role, msg.content, imageDataUrl);
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

        // Capture pending image before clearing
        const imgData = pendingImageData;
        const imgType = pendingImageType;
        let userImageDataUrl = null;
        if (imgData && imgType) {
            userImageDataUrl = `data:${imgType};base64,${imgData}`;
        }

        addMessage("user", text, userImageDataUrl);
        input.value = "";
        clearPreview();
        setEnabled(false);

        const bubble = createAssistantBubble();

        try {
            const payload = { message: text };
            if (imgData && imgType) {
                payload.image_data = imgData;
                payload.image_media_type = imgType;
            }

            const res = await fetch("/api/chat", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
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

                    if (event.type === "web_search") {
                        // Show searching indicator before response text
                        const indicator = document.createElement("div");
                        indicator.className = "web-search-indicator";
                        indicator.textContent = "Searching the web\u2026";
                        bubble.appendChild(indicator);
                        scrollToBottom();
                    } else if (event.type === "fetching_url") {
                        // Show URL fetch indicator
                        const indicator = document.createElement("div");
                        indicator.className = "web-search-indicator";
                        indicator.textContent = "Reading URL\u2026";
                        bubble.appendChild(indicator);
                        scrollToBottom();
                    } else if (event.type === "reading_file") {
                        // Show file read indicator
                        const indicator = document.createElement("div");
                        indicator.className = "web-search-indicator";
                        indicator.textContent = "Reading file\u2026";
                        bubble.appendChild(indicator);
                        scrollToBottom();
                    } else if (event.type === "token") {
                        // Remove any status indicators once text starts arriving
                        for (const ind of bubble.querySelectorAll(".web-search-indicator")) {
                            ind.remove();
                        }
                        bubble.textContent += event.text;
                        scrollToBottom();
                    } else if (event.type === "error") {
                        bubble.textContent += event.detail || "Unknown error";
                        bubble.className = "message error";
                    } else if (event.type === "done") {
                        // Convert markdown links to clickable <a> tags
                        const raw = bubble.textContent;
                        bubble.textContent = "";
                        for (const node of renderMarkdownLinks(raw)) {
                            bubble.appendChild(node);
                        }
                        // Re-enable input immediately — don't wait for
                        // stream close (curator may still be running)
                        setEnabled(true);
                        input.focus();
                    }
                }
            }
        } catch (err) {
            bubble.textContent = "Connection error: " + err.message;
            bubble.className = "message error";
        } finally {
            // Safety net — always re-enable even if something unexpected happened
            setEnabled(true);
            input.focus();
        }
    });
});
