import JSZip from 'jszip'

/** Zip audio so the server can extract and run Hear + Claude in parallel. */
export async function zipAudioFiles(files: File[]): Promise<Blob> {
  if (!files || files.length < 2) {
    throw new Error('Zip is only used when importing more than one file.')
  }
  const zip = new JSZip()
  files.forEach((f, i) => {
    const name = String(f.name || `file-${i + 1}.mp3`).replace(/[/\\]/g, '_')
    zip.file(`${String(i).padStart(2, '0')}_${name}`, f)
  })
  return zip.generateAsync({ type: 'blob', compression: 'DEFLATE', compressionOptions: { level: 6 } })
}
