// SafeStop Driver Camera Verification & Safe Offline Queue Sync Engine

class DriverCameraScanner {
    constructor(videoElementId, canvasElementId, tripId) {
        this.video = document.getElementById(videoElementId);
        this.canvas = document.getElementById(canvasElementId);
        this.tripId = tripId;
        this.stream = null;
        this.isScanning = false;
        this.queueKey = 'saferide_offline_queue';
        this.maxBatchSize = 50;

        // Initialize Speech Synthesis for Driver Hands-Free Voice Guidance
        this.synth = window.speechSynthesis || null;

        // Flush any pre-existing queued items safely without deleting unprocessed records
        window.addEventListener('online', () => this.checkAndSyncOfflineQueue());
        this.checkAndSyncOfflineQueue();
    }

    getCsrfToken() {
        const meta = document.querySelector('meta[name="csrf-token"]');
        return meta ? meta.getAttribute('content') || '' : '';
    }

    getScanAction() {
        const selected = document.querySelector('input[name="scan_action"]:checked');
        return selected && selected.value === 'DROP' ? 'DROP' : 'BOARD';
    }

    generateEventId() {
        if (window.crypto && typeof window.crypto.randomUUID === 'function') {
            return window.crypto.randomUUID();
        }
        return `evt-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
    }

    async startCamera() {
        try {
            this.stream = await navigator.mediaDevices.getUserMedia({
                video: { facingMode: "environment", width: { ideal: 640 }, height: { ideal: 480 } },
                audio: false
            });
            this.video.srcObject = this.stream;
            await this.video.play();
            return true;
        } catch (err) {
            console.error("Error accessing camera:", err);
            alert("Unable to access smartphone camera. Please check camera permissions.");
            return false;
        }
    }

    stopCamera() {
        if (this.stream) {
            this.stream.getTracks().forEach(track => track.stop());
        }
    }

    captureFrameBase64() {
        if (!this.video || this.video.paused || this.video.ended) return null;

        const context = this.canvas.getContext('2d');
        this.canvas.width = this.video.videoWidth || 640;
        this.canvas.height = this.video.videoHeight || 480;
        context.drawImage(this.video, 0, 0, this.canvas.width, this.canvas.height);

        return this.canvas.toDataURL('image/jpeg', 0.85);
    }

    async scanAndVerify() {
        const frameB64 = this.captureFrameBase64();
        if (!frameB64) {
            return { status: 'ERROR', message: 'Camera feed not active' };
        }

        const scanAction = this.getScanAction();
        const clientEventId = this.generateEventId();
        const resultDisplay = document.getElementById('scan-result-card');
        if (resultDisplay) {
            resultDisplay.className = 'glass-card p-3 mb-3 text-center border-info';
            resultDisplay.innerHTML = `<div class="spinner-border spinner-border-sm text-info me-2"></div> Verifying student with server...`;
        }

        try {
            const response = await fetch('/driver/api/verify-face', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': this.getCsrfToken()
                },
                body: JSON.stringify({
                    image: frameB64,
                    trip_id: this.tripId,
                    scan_action: scanAction,
                    client_event_id: clientEventId
                })
            });

            const data = await response.json().catch(() => ({
                status: 'ERROR',
                message: 'Invalid server response.'
            }));

            if (!response.ok) {
                this.handleScanError(data.message || `Server rejected verification (HTTP ${response.status}).`);
                return data;
            }

            this.handleScanResult(data);
            return data;
        } catch (err) {
            // Privacy safeguard: Do NOT store raw student biometric face images in browser localStorage.
            if (resultDisplay) {
                resultDisplay.className = 'glass-card p-3 mb-3 text-center border-warning bg-warning bg-opacity-10';
                resultDisplay.innerHTML = `
                    <div class="h6 text-warning fw-bold mb-1">📶 OFFLINE — FACE VERIFICATION UNAVAILABLE</div>
                    <div class="small text-white-50 mb-2">
                        For student biometric privacy, raw face images cannot be stored offline on this device.
                        Reconnect to the network or use Manual Driver Verification.
                    </div>
                    <button type="button" class="btn btn-sm btn-outline-light" data-bs-toggle="modal" data-bs-target="#manualModal">
                        <i class="fa-solid fa-keyboard me-1"></i> Open Manual Driver Verification
                    </button>
                `;
            }
            this.speakVoiceAlert("Network unavailable. Offline face verification is disabled. Use manual verification.");
            return { status: 'OFFLINE_UNAVAILABLE', message: 'Offline biometric verification is disabled for privacy.' };
        }
    }

    handleScanError(message) {
        const resultDisplay = document.getElementById('scan-result-card');
        if (!resultDisplay) return;
        resultDisplay.className = 'glass-card p-3 mb-3 text-center border-danger bg-danger bg-opacity-10';
        resultDisplay.innerHTML = `
            <div class="h6 text-danger fw-bold mb-1">❌ Verification Not Confirmed</div>
            <div class="text-white-50 small">${this.escapeHtml(message)}</div>
        `;
    }

    handleScanResult(data) {
        const resultDisplay = document.getElementById('scan-result-card');
        if (!resultDisplay) return;

        if (data.status === 'VERIFIED' || data.status === 'DROP_VERIFIED') {
            const label = data.status === 'DROP_VERIFIED' ? '✅ DROP-OFF VERIFIED' : '✅ STUDENT VERIFIED';
            resultDisplay.className = 'glass-card p-3 mb-3 text-center border-success bg-success bg-opacity-10';
            resultDisplay.innerHTML = `
                <div class="h4 text-success fw-bold mb-1">${label}</div>
                <div class="h5 text-white">${this.escapeHtml(data.student_name)} (${this.escapeHtml(data.roll_number)})</div>
                <div class="text-muted small">Confidence: ${data.confidence}% • Server Confirmed</div>
            `;
            this.playAudioAlert('success');
            this.speakVoiceAlert(`Verified: ${data.student_name}`);
            setTimeout(() => window.location.reload(), 1800);

        } else if (data.status === 'WRONG_BUS') {
            resultDisplay.className = 'glass-card p-3 mb-3 text-center border-danger bg-danger bg-opacity-25 animate__animated animate__shakeX';
            resultDisplay.innerHTML = `
                <div class="h3 text-danger fw-bold mb-1">🚨 WRONG BUS DETECTED!</div>
                <div class="h4 text-white">${this.escapeHtml(data.student_name)}</div>
                <div class="badge bg-danger p-2 fs-6 my-2">Assigned to ${this.escapeHtml(data.assigned_bus)} • Boarded ${this.escapeHtml(data.current_bus)}</div>
                <div class="text-white-50 small">Critical safety alert dispatched to parent and transport admin.</div>
            `;
            this.playAudioAlert('danger');
            this.speakVoiceAlert(`Warning! Wrong Bus Detected! ${data.student_name} belongs on ${data.assigned_bus}`);

        } else if (data.status === 'ALREADY_BOARDED' || data.status === 'ALREADY_DROPPED') {
            resultDisplay.className = 'glass-card p-3 mb-3 text-center border-warning bg-warning bg-opacity-10';
            resultDisplay.innerHTML = `
                <div class="h5 text-warning fw-bold mb-1">ℹ️ ALREADY RECORDED</div>
                <div class="text-white">${this.escapeHtml(data.message || `${data.student_name} is already recorded on this trip.`)}</div>
            `;
            this.speakVoiceAlert(`${data.student_name} is already recorded.`);

        } else {
            resultDisplay.className = 'glass-card p-3 mb-3 text-center border-warning';
            resultDisplay.innerHTML = `
                <div class="h6 text-warning mb-1">⚠️ Not Verified</div>
                <div class="text-muted small">${this.escapeHtml(data.message || 'Please position student face inside circle.')}</div>
            `;
        }
    }

    escapeHtml(value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    readStoredQueue() {
        const raw = localStorage.getItem(this.queueKey);
        if (!raw) return { items: [], malformed: false };
        try {
            const parsed = JSON.parse(raw);
            if (!Array.isArray(parsed)) {
                return { items: [], malformed: true };
            }
            let mutated = false;
            const normalized = parsed.map(item => {
                if (!item || typeof item !== 'object') {
                    mutated = true;
                    return { id: this.generateEventId(), _malformedItem: true };
                }
                if (!item.id && !item.client_event_id) {
                    mutated = true;
                    return { ...item, id: this.generateEventId(), client_event_id: this.generateEventId() };
                }
                const stableId = item.client_event_id || item.id;
                if (!item.id || !item.client_event_id) {
                    mutated = true;
                    return { ...item, id: stableId, client_event_id: stableId };
                }
                return item;
            });
            if (mutated) {
                localStorage.setItem(this.queueKey, JSON.stringify(normalized));
            }
            return { items: normalized, malformed: false };
        } catch (err) {
            console.warn("Malformed offline queue data detected; skipping auto-sync without crash.");
            return { items: [], malformed: true };
        }
    }

    updateSyncBanner(statusType, message) {
        const banner = document.getElementById('offline-sync-banner');
        if (!banner) return;
        if (!message) {
            banner.classList.add('d-none');
            banner.innerHTML = '';
            return;
        }
        const clsMap = {
            info: 'alert-info',
            success: 'alert-success',
            warning: 'alert-warning',
            danger: 'alert-danger'
        };
        banner.className = `alert ${clsMap[statusType] || 'alert-info'} small mb-3`;
        banner.innerHTML = message;
    }

    async checkAndSyncOfflineQueue() {
        const { items, malformed } = this.readStoredQueue();
        if (malformed) {
            this.updateSyncBanner(
                'warning',
                '<strong>⚠️ Corrupted local queue detected:</strong> Existing offline data could not be parsed cleanly. Please verify attendance manually.'
            );
            return;
        }
        if (items.length === 0) {
            this.updateSyncBanner(null, '');
            return;
        }

        const retryableCandidates = items.filter(it => !it._syncStatus || it._syncStatus === 'retryable_failure');
        if (retryableCandidates.length === 0) {
            const rejectedCount = items.filter(it => it._syncStatus === 'rejected').length;
            if (rejectedCount > 0) {
                this.updateSyncBanner(
                    'danger',
                    `<strong>⚠️ Offline Sync Attention Required:</strong> ${rejectedCount} queued item(s) were rejected by the server. Please verify manually.`
                );
            }
            return;
        }

        const batch = retryableCandidates.slice(0, this.maxBatchSize);
        this.updateSyncBanner(
            'info',
            `<span class="spinner-border spinner-border-sm me-1"></span> Syncing ${batch.length} pending queued item(s) (${items.length} total in local queue)...`
        );

        try {
            const resp = await fetch('/driver/api/sync-offline-queue', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': this.getCsrfToken()
                },
                body: JSON.stringify({ queue: batch })
            });

            if (!resp.ok) {
                this.updateSyncBanner(
                    'warning',
                    `<strong>⏳ Sync Deferred (HTTP ${resp.status}):</strong> Retained ${items.length} pending item(s) for retry.`
                );
                return;
            }

            const data = await resp.json();
            const resultById = new Map();
            (data.results || []).forEach(r => {
                if (r && r.id) {
                    resultById.set(r.id, r);
                }
            });

            let accepted = 0;
            let alreadyProcessed = 0;
            let rejected = 0;
            let retryable = 0;
            const remaining = [];

            for (const item of items) {
                const itemId = item.client_event_id || item.id;
                const res = resultById.get(itemId);
                if (!res) {
                    // Unprocessed item: retain safely
                    remaining.push(item);
                    retryable += 1;
                    continue;
                }
                if (res.status === 'accepted') {
                    accepted += 1;
                } else if (res.status === 'already_processed') {
                    alreadyProcessed += 1;
                } else if (res.status === 'rejected') {
                    rejected += 1;
                    remaining.push({ ...item, _syncStatus: 'rejected', _syncError: res.message || 'Rejected' });
                } else {
                    retryable += 1;
                    remaining.push({ ...item, _syncStatus: 'retryable_failure', _syncError: res.message || 'Retryable failure' });
                }
            }

            if (remaining.length === 0) {
                localStorage.removeItem(this.queueKey);
                this.updateSyncBanner(
                    'success',
                    `<strong>✅ Offline Queue Synced:</strong> ${accepted} accepted, ${alreadyProcessed} already processed.`
                );
            } else {
                localStorage.setItem(this.queueKey, JSON.stringify(remaining));
                const tone = rejected > 0 ? 'danger' : 'warning';
                this.updateSyncBanner(
                    tone,
                    `<strong>Sync Summary:</strong> ${accepted} accepted, ${alreadyProcessed} already processed, ${retryable} pending retry, ${rejected} rejected.`
                );
            }
        } catch (e) {
            this.updateSyncBanner(
                'warning',
                `<strong>📶 Offline:</strong> ${items.length} queued item(s) retained locally until connection returns.`
            );
        }
    }

    speakVoiceAlert(text) {
        if (!this.synth) return;
        try {
            this.synth.cancel();
            const utterance = new SpeechSynthesisUtterance(text);
            utterance.rate = 1.0;
            utterance.pitch = 1.0;
            utterance.volume = 1.0;
            this.synth.speak(utterance);
        } catch (e) {
            console.log("Web Speech API voice assist error:", e);
        }
    }

    playAudioAlert(type) {
        try {
            const ctx = new (window.AudioContext || window.webkitAudioContext)();
            const osc = ctx.createOscillator();
            const gain = ctx.createGain();
            osc.connect(gain);
            gain.connect(ctx.destination);

            if (type === 'success') {
                osc.frequency.setValueAtTime(587.33, ctx.currentTime);
                osc.frequency.setValueAtTime(880, ctx.currentTime + 0.1);
                gain.gain.setValueAtTime(0.2, ctx.currentTime);
                osc.start();
                osc.stop(ctx.currentTime + 0.3);
            } else if (type === 'danger') {
                osc.type = 'sawtooth';
                osc.frequency.setValueAtTime(300, ctx.currentTime);
                osc.frequency.setValueAtTime(150, ctx.currentTime + 0.2);
                gain.gain.setValueAtTime(0.4, ctx.currentTime);
                osc.start();
                osc.stop(ctx.currentTime + 0.5);
            }
        } catch (e) {
            console.log("Web Audio API error:", e);
        }
    }
}
