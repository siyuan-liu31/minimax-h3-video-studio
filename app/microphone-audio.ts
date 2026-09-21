/** Browser microphone recordings are converted to the server's existing WAV asset contract. */

export const MAX_MICROPHONE_SECONDS = 5 * 60;
const MAX_WAV_BYTES = 256 * 1024 * 1024;

type AudioSamples = Pick<AudioBuffer, "length" | "sampleRate" | "numberOfChannels" | "getChannelData">;

export function encodeMicrophoneWav(audio: AudioSamples): Blob {
  const { length, sampleRate, numberOfChannels } = audio;
  if (!Number.isInteger(length) || length < 1 || !Number.isInteger(sampleRate) || sampleRate < 8_000 || sampleRate > 192_000 ||
      !Number.isInteger(numberOfChannels) || numberOfChannels < 1 || numberOfChannels > 8) {
    throw new Error("录音音频数据无效");
  }
  // PCM16 mono is the voice worker's input format; retain the device's sample rate.
  const byteLength = 44 + length * 2;
  if (byteLength > MAX_WAV_BYTES) throw new Error("录音过大，请缩短录制时间");
  const channels = Array.from({ length: numberOfChannels }, (_, index) => audio.getChannelData(index));
  if (channels.some((channel) => channel.length !== length)) throw new Error("录音音频数据无效");
  const bytes = new ArrayBuffer(byteLength);
  const view = new DataView(bytes);
  function ascii(offset: number, value: string) {
    for (let index = 0; index < value.length; index += 1) view.setUint8(offset + index, value.charCodeAt(index));
  }
  ascii(0, "RIFF"); view.setUint32(4, byteLength - 8, true); ascii(8, "WAVE");
  ascii(12, "fmt "); view.setUint32(16, 16, true); view.setUint16(20, 1, true);
  view.setUint16(22, 1, true); view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true);
  ascii(36, "data"); view.setUint32(40, length * 2, true);
  for (let index = 0; index < length; index += 1) {
    let mixed = 0;
    for (const channel of channels) mixed += channel[index];
    const sample = Math.max(-1, Math.min(1, mixed / numberOfChannels));
    view.setInt16(44 + index * 2, Math.round(sample < 0 ? sample * 32768 : sample * 32767), true);
  }
  return new Blob([bytes], { type: "audio/wav" });
}

export async function recordedMicrophoneFile(recorded: Blob, at = new Date()): Promise<File> {
  if (recorded.size === 0) throw new Error("没有录到声音，请重试");
  const context = new AudioContext();
  try {
    const decoded = await context.decodeAudioData(await recorded.arrayBuffer());
    const wav = encodeMicrophoneWav(decoded);
    const stamp = at.toISOString().slice(0, 19).replace(/[-:T]/g, "");
    return new File([wav], `microphone-${stamp}.wav`, { type: "audio/wav" });
  } finally {
    await context.close();
  }
}
