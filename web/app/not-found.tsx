import Link from "next/link";
import { ArrowLeft, FileQuestion } from "lucide-react";
import { Button } from "@/components/ui/button";

export default function NotFound() {
  return (
    <main className="flex min-h-dvh items-center justify-center bg-background px-6">
      <div className="max-w-sm text-center"><span className="mx-auto flex size-10 items-center justify-center rounded-[5px] border border-border bg-surface text-muted-foreground"><FileQuestion className="size-5" /></span><p className="mt-4 font-mono text-xs text-muted-foreground">404</p><h1 className="mt-1 text-xl font-semibold">找不到这个页面</h1><p className="mt-2 text-sm leading-6 text-muted-foreground">链接可能已失效，或者你没有访问该资源的权限。</p><Button asChild variant="secondary" className="mt-5"><Link href="/knowledge"><ArrowLeft className="size-4" />返回知识</Link></Button></div>
    </main>
  );
}
