/* Voice input: record from the microphone and turn it into a small WAV (16 kHz mono) for the server's
 * transcription model. Browsers record WebM/MP4; the models take WAV/MP3, so the conversion happens here. */

export class Recorder {
  private rec: MediaRecorder | null = null;
  private chunks: Blob[] = [];
  private stream: MediaStream | null = null;
  private ctx: AudioContext | null = null;
  private analyser: AnalyserNode | null = null;
  started = 0;

  async start() {
    this.stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
    this.chunks = [];
    this.rec = new MediaRecorder(this.stream);
    this.rec.ondataavailable = (e) => { if (e.data.size) this.chunks.push(e.data); };
    this.rec.start(250);
    this.started = Date.now();
    this.ctx = new AudioContext();
    this.analyser = this.ctx.createAnalyser();
    this.analyser.fftSize = 512;
    this.ctx.createMediaStreamSource(this.stream).connect(this.analyser);
  }

  /** 0..1, for the level meter */
  level(): number {
    if (!this.analyser) return 0;
    const a = new Uint8Array(this.analyser.fftSize);
    this.analyser.getByteTimeDomainData(a);
    let peak = 0;
    for (const v of a) peak = Math.max(peak, Math.abs(v - 128));
    return Math.min(1, peak / 64);
  }

  private release() {
    this.stream?.getTracks().forEach((t) => t.stop());
    void this.ctx?.close();
    this.stream = null; this.ctx = null; this.analyser = null;
  }

  cancel() { try { this.rec?.stop(); } catch { /* not started */ } this.release(); }

  async stop(): Promise<Blob> {
    const rec = this.rec;
    if (!rec) throw new Error("not recording");
    const done = new Promise<void>((resolve) => { rec.onstop = () => resolve(); });
    rec.stop();
    await done;
    this.release();
    const raw = new Blob(this.chunks, { type: rec.mimeType || "audio/webm" });
    return toWav(raw);
  }
}

export async function toWav(blob: Blob, rate = 16000): Promise<Blob> {
  const decoded = await new AudioContext().decodeAudioData(await blob.arrayBuffer());
  const offline = new OfflineAudioContext(1, Math.ceil(decoded.duration * rate), rate);
  const src = offline.createBufferSource();
  src.buffer = decoded;
  src.connect(offline.destination);
  src.start();
  const pcm = (await offline.startRendering()).getChannelData(0);
  const buf = new ArrayBuffer(44 + pcm.length * 2);
  const v = new DataView(buf);
  const w = (o: number, s: string) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
  w(0, "RIFF"); v.setUint32(4, 36 + pcm.length * 2, true); w(8, "WAVE"); w(12, "fmt ");
  v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true); v.setUint32(24, rate, true);
  v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true); w(36, "data");
  v.setUint32(40, pcm.length * 2, true);
  for (let i = 0; i < pcm.length; i++) v.setInt16(44 + i * 2, Math.max(-1, Math.min(1, pcm[i]!)) * 0x7fff, true);
  return new Blob([buf], { type: "audio/wav" });
}
