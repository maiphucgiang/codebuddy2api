import { Unzip, UnzipInflate } from "fflate";
export const MAX_FILE = 1024 * 1024;
export const MAX_TOTAL = 32 * MAX_FILE;
export type ImportFile = { name: string; content: string };
export type ImportResult = { name: string; ok: boolean; error?: string };
export function validName(name: string) {
  return (
    name.endsWith(".info") &&
    !/[\\/]/.test(name) &&
    !Array.from(name).some((char) => char.charCodeAt(0) < 32) &&
    name !== ".info"
  );
}
export async function prepareImports(
  files: File[],
): Promise<{ files: ImportFile[]; results: ImportResult[] }> {
  const accepted: ImportFile[] = [],
    results: ImportResult[] = [];
  let total = 0,
    count = 0;
  const names = new Set<string>();
  const accept = (name: string, data: Uint8Array) => {
    if (!validName(name)) {
      results.push({ name, ok: false, error: "只允许 ZIP 根目录中的安全 .info 文件名" });
      return;
    }
    if (names.has(name)) {
      results.push({ name, ok: false, error: "批次中存在同名文件" });
      return;
    }
    names.add(name);
    try {
      accepted.push({ name, content: new TextDecoder("utf-8", { fatal: true }).decode(data) });
    } catch {
      results.push({ name, ok: false, error: "文件不是有效 UTF-8" });
    }
  };
  if (files.reduce((sum, f) => sum + f.size, 0) > MAX_TOTAL)
    throw new Error("压缩文件及上传总大小不可超过 32 MiB");
  for (const file of files) {
    if (file.name.toLowerCase().endsWith(".zip")) {
      let failure: string | null = null;
      let pending = 0;
      const unzip = new Unzip((entry) => {
        if (entry.name.endsWith("/")) return;
        count++;
        if (count > 100) throw new Error("每批最多 100 项");
        if (!validName(entry.name)) {
          results.push({ name: entry.name, ok: false, error: "不支持嵌套路径或非 .info 文件" });
          return;
        }
        let size = 0;
        pending++;
        const chunks: Uint8Array[] = [];
        entry.ondata = (error, data, final) => {
          if (failure) return;
          if (error) {
            failure = `ZIP 文件损坏：${entry.name}`;
            return;
          }
          size += data.length;
          total += data.length;
          if (size > MAX_FILE || total > MAX_TOTAL) {
            failure = "解压超限：单项最多 1 MiB，批量最多 32 MiB";
            entry.terminate();
            return;
          }
          chunks.push(data);
          if (final) {
            pending--;
            const joined = new Uint8Array(size);
            let offset = 0;
            for (const chunk of chunks) {
              joined.set(chunk, offset);
              offset += chunk.length;
            }
            accept(entry.name, joined);
          }
        };
        entry.start();
      });
      unzip.register(UnzipInflate);
      const data = new Uint8Array(await file.arrayBuffer());
      // Small compressed chunks bound transient inflation allocations for hostile archives.
      for (let offset = 0; offset < data.length; offset += 4096) {
        unzip.push(data.subarray(offset, offset + 4096), offset + 4096 >= data.length);
        if (failure) throw new Error(failure);
      }
      if (pending) throw new Error("ZIP 文件不完整，未上传任何文件");
    } else {
      count++;
      total += file.size;
      if (count > 100 || total > MAX_TOTAL) throw new Error("每批最多 100 项 / 32 MiB");
      if (file.size > MAX_FILE) {
        results.push({ name: file.name, ok: false, error: "单文件超过 1 MiB" });
        continue;
      }
      accept(file.name, new Uint8Array(await file.arrayBuffer()));
    }
  }
  if (!count) throw new Error("ZIP 中没有可导入文件");
  return { files: accepted, results };
}
