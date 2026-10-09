"""One browser-local audio handler for both task views."""

BUZZER_CONTROLS = """
<div style="display:flex;align-items:center;gap:16px;flex-wrap:wrap">
  <button type="button" data-plasma-buzzer-test
    style="padding:8px 16px;border:1px solid var(--border-color-primary);border-radius:8px;
           background:var(--button-secondary-background-fill);color:var(--button-secondary-text-color)">
    Test buzzer</button>
  <span data-plasma-buzzer-status role="status" style="font-size:12px"></span>
</div>
"""

BUZZER_JS = r"""
    let audioContext = null;
    let lastCompletion = null;
    let buzzerMessage = '';
    const buzzerStatus = (message) => {
        buzzerMessage = message;
        document.querySelectorAll('[data-plasma-buzzer-status]').forEach(el => {
            if (el.textContent !== message) el.textContent = message;
        });
    };
    const syncBuzzer = () => {
        document.querySelectorAll('[data-plasma-buzzer-status]').forEach(el => {
            if (el.textContent !== buzzerMessage) el.textContent = buzzerMessage;
        });
    };
    const unlockAudio = () => {
        try {
            const Audio = window.AudioContext || window.webkitAudioContext;
            if (!Audio) throw new Error('Web Audio unavailable');
            if (!audioContext) audioContext = new Audio();
            if (audioContext.state !== 'running') {
                audioContext.resume().catch(() => buzzerStatus('Click Test buzzer to enable audio.'));
            }
        } catch (_) {
            buzzerStatus('Browser audio unavailable.');
        }
    };
    const playBuzzer = async (gesture = false) => {
        if (!gesture && (!audioContext || audioContext.state !== 'running')) {
            buzzerStatus('Click Test buzzer to enable audio.');
            return;
        }
        unlockAudio();
        if (!audioContext) return;
        try {
            await audioContext.resume();
            if (audioContext.state !== 'running') throw new Error('Audio blocked');
            const now = audioContext.currentTime;
            for (const offset of [0, 0.3]) {
                const oscillator = audioContext.createOscillator();
                const gain = audioContext.createGain();
                oscillator.type = 'sine';
                oscillator.frequency.value = 880;
                gain.gain.setValueAtTime(0, now + offset);
                gain.gain.linearRampToValueAtTime(0.2, now + offset + 0.015);
                gain.gain.linearRampToValueAtTime(0, now + offset + 0.22);
                oscillator.connect(gain);
                gain.connect(audioContext.destination);
                oscillator.onended = () => { oscillator.disconnect(); gain.disconnect(); };
                oscillator.start(now + offset);
                oscillator.stop(now + offset + 0.23);
            }
            buzzerStatus('Buzzer played.');
        } catch (_) {
            buzzerStatus('Click Test buzzer to enable audio.');
        }
    };
    const buzzerClick = (event) => {
        if (event.target.closest('[data-plasma-buzzer-test]')) {
            playBuzzer(true);
        } else if (event.target.closest('#plasma-task-start, #plasma-focus-task-start')) {
            unlockAudio();
        }
    };
    const checkCompletion = () => {
        const event = document.querySelector('#plasma-buzzer-event [data-completion-id]');
        if (!event) return;
        const id = Number(event.dataset.completionId);
        if (!Number.isFinite(id)) return;
        const previous = lastCompletion;
        lastCompletion = id;
        // First delivery is the baseline; never replay an old completion on load.
        if (previous !== null && id > previous) playBuzzer();
    };
    const buzzerObserver = new MutationObserver(() => { syncBuzzer(); checkCompletion(); });
    buzzerObserver.observe(document.body, {childList: true, subtree: true});
    syncBuzzer();
    checkCompletion();
    document.addEventListener('click', buzzerClick);
    window.addEventListener('pagehide', () => {
        buzzerObserver.disconnect();
        document.removeEventListener('click', buzzerClick);
        if (audioContext) audioContext.close();
    }, {once: true});
"""
