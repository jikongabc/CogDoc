import { redirect } from "next/navigation";

export default async function LegacySourcesPage({ params }: { params: Promise<{ kbId: string }> }) {
  const { kbId } = await params;
  redirect(`/integrations?kb=${encodeURIComponent(kbId)}`);
}
