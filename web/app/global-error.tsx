"use client";

import { useEffect } from "react";
import { AlertTriangle, RotateCcw } from "lucide-react";

export default function GlobalError({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  useEffect(() => {
    console.error("CogDoc UI failure", { name: error.name, digest: error.digest });
  }, [error]);
  return (
    <html lang="zh-CN">
      <body className="bg-[#F7F8FA] text-[#172033]">
        <main className="flex min-h-dvh items-center justify-center px-6">
          <div className="w-full max-w-md border-l-2 border-[#B42318] bg-white px-5 py-4 shadow-sm">
            <AlertTriangle className="size-5 text-[#B42318]" />
            <h1 className="mt-3 text-lg font-semibold">工作台暂时无法显示</h1>
            <p className="mt-1 text-sm text-[#667085]">当前页面遇到意外错误。你的后端数据没有因此改变。</p>
            <button className="mt-4 inline-flex h-9 items-center gap-2 rounded-[5px] bg-[#254F8F] px-3 text-sm font-medium text-white hover:bg-[#1E4278]" onClick={reset}><RotateCcw className="size-4" />重新载入</button>
          </div>
        </main>
      </body>
    </html>
  );
}
