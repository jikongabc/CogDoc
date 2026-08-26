"use client";

import { useEffect } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Building2, ExternalLink, Fingerprint, Link2, Unlink } from "lucide-react";
import { useForm, useWatch } from "react-hook-form";
import { toast } from "sonner";
import { AdminPageFrame } from "@/components/admin/admin-nav";
import { PageHeader } from "@/components/layout/page-header";
import { EmptyState } from "@/components/data-display/empty-state";
import { QueryState } from "@/components/data-display/query-state";
import { StatusBadge } from "@/components/data-display/status-badge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { api } from "@/lib/api/client";
import { controlApi, isRecord, numberValue, records, textValue, type JsonRecord } from "@/lib/api/control-plane";
import { queryKeys } from "@/lib/query/keys";
import { useSessionStore } from "@/stores/session-store";

interface PolicyForm {
  domains: string;
  role: string;
  groupClaim: string;
  enabled: boolean;
  requireMappedGroup: boolean;
}

export default function IdentityPage() {
  const queryClient = useQueryClient();
  const workspaceId = useSessionStore((state) => state.workspace?.workspace_id || "");
  const config = useQuery({ queryKey: queryKeys.authConfig, queryFn: api.authConfig });
  const identities = useQuery({ queryKey: ["admin", "oidc-identities"], queryFn: controlApi.oidcIdentities, enabled: Boolean(workspaceId) && Boolean(config.data?.oidc_enabled), retry: false });
  const policy = useQuery({ queryKey: ["admin", "oidc-policy", workspaceId], queryFn: () => controlApi.oidcPolicy(workspaceId), enabled: Boolean(workspaceId) && Boolean(config.data?.oidc_enabled), retry: false });
  const scim = useQuery({ queryKey: ["admin", "scim", workspaceId], queryFn: () => controlApi.scimStatus(workspaceId), enabled: Boolean(workspaceId) && Boolean(config.data?.scim_enabled), retry: false });
  const policyEnvelope = isRecord(policy.data) ? policy.data : {};
  const policyRow = isRecord(policyEnvelope.policy) ? policyEnvelope.policy : policyEnvelope;
  const form = useForm<PolicyForm>({ defaultValues: { domains: "", role: "viewer", groupClaim: "groups", enabled: false, requireMappedGroup: false } });
  const role = useWatch({ control: form.control, name: "role" });
  useEffect(() => {
    if (!policy.data) return;
    form.reset({
      domains: Array.isArray(policyRow.allowed_domains) ? policyRow.allowed_domains.filter((item): item is string => typeof item === "string").join(", ") : "",
      role: textValue(policyRow.default_role, "viewer"),
      groupClaim: textValue(policyRow.group_claim, "groups"),
      enabled: Boolean(policyRow.enabled),
      requireMappedGroup: Boolean(policyRow.require_mapped_group),
    });
  }, [form, policy.data, policyRow.allowed_domains, policyRow.default_role, policyRow.enabled, policyRow.group_claim, policyRow.require_mapped_group]);
  const save = useMutation({
    mutationFn: (values: PolicyForm) => controlApi.updateOidcPolicy(workspaceId, {
      allowed_domains: values.domains.split(",").map((item) => item.trim()).filter(Boolean),
      default_role: values.role,
      enabled: values.enabled,
      group_claim: values.groupClaim,
      group_role_map: isRecord(policyRow.group_role_map) ? policyRow.group_role_map : {},
      require_mapped_group: values.requireMappedGroup,
      expected_revision: numberValue(policyRow.revision) || undefined,
    }),
    onSuccess: async () => { toast.success("企业身份策略已保存"); await queryClient.invalidateQueries({ queryKey: ["admin", "oidc-policy"] }); },
    onError: (error) => toast.error(error.message),
  });
  const unlink = useMutation({
    mutationFn: (id: string) => controlApi.unlinkOidcIdentity(id),
    onSuccess: async () => { toast.success("企业身份已解除绑定"); await queryClient.invalidateQueries({ queryKey: ["admin", "oidc-identities"] }); },
    onError: (error) => toast.error(error.message),
  });
  const startLink = async () => {
    try {
      const value = await controlApi.startOidcLink(`${window.location.origin}/admin/identity`);
      const row = isRecord(value) ? value : {};
      const url = textValue(row.authorization_url, "");
      if (!url) throw new Error("身份服务未返回授权地址");
      window.location.assign(url);
    } catch (error) { toast.error(error instanceof Error ? error.message : "无法绑定企业身份"); }
  };
  const identityRows = records(identities.data, ["items", "identities"]);
  const scimEnvelope = isRecord(scim.data) ? scim.data : {};
  const scimPayload = isRecord(scimEnvelope.status) ? scimEnvelope.status : scimEnvelope;
  const scimRow: JsonRecord = { ...scimPayload, status: Boolean(scimPayload.enabled) ? "active" : "disabled" };
  return (
    <AdminPageFrame>
      <PageHeader eyebrow="Identity & provisioning" title="企业身份" description="配置 OIDC 准入、账号绑定和 SCIM 配置状态。" actions={config.data?.oidc_enabled && workspaceId ? <Button onClick={() => void startLink()}><Link2 className="size-4" />绑定企业身份</Button> : undefined} />
      {!workspaceId ? <EmptyState icon={Fingerprint} title="账号认证未启用" description="本地兼容模式不创建企业身份目录。启用账号认证后可配置 OIDC 与 SCIM。" /> : <div className="space-y-6 p-4 md:p-6">
        <section className="border border-border bg-surface"><div className="flex h-11 items-center justify-between border-b border-border px-4"><div><h3 className="text-[13px] font-semibold">OIDC 单点登录</h3><p className="text-[11px] text-muted-foreground">{config.data?.oidc_display_name || "Enterprise SSO"}</p></div><StatusBadge status={config.data?.oidc_enabled ? "active" : "disabled"} /></div>{!config.data?.oidc_enabled ? <EmptyState icon={Building2} compact title="OIDC 未配置" description="在服务端配置身份提供方后，这里会开放准入策略和账号绑定。" /> : <form className="grid gap-4 p-4 md:grid-cols-2" onSubmit={form.handleSubmit((values) => save.mutate(values))}><div className="space-y-1.5 md:col-span-2"><Label htmlFor="oidc-domains">允许的邮箱域名</Label><Input id="oidc-domains" placeholder="company.com, subsidiary.com" {...form.register("domains")} /></div><div className="space-y-1.5"><Label>默认角色</Label><Select value={role} onValueChange={(value) => form.setValue("role", value)}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent>{["viewer", "reviewer", "editor", "admin"].map((item) => <SelectItem key={item} value={item}>{item}</SelectItem>)}</SelectContent></Select></div><div className="space-y-1.5"><Label htmlFor="group-claim">组声明字段</Label><Input id="group-claim" {...form.register("groupClaim")} /></div><label className="flex items-center gap-2 text-[13px]"><input type="checkbox" className="size-4 accent-primary" {...form.register("enabled")} />允许企业账号加入此工作区</label><label className="flex items-center gap-2 text-[13px]"><input type="checkbox" className="size-4 accent-primary" {...form.register("requireMappedGroup")} />必须匹配已配置的组织组</label><div className="md:col-span-2"><Button type="submit" variant="primary" loading={save.isPending}>保存身份策略</Button></div></form>}</section>
        {config.data?.oidc_enabled ? <section className="border border-border bg-surface"><div className="flex h-11 items-center justify-between border-b border-border px-4"><h3 className="text-[13px] font-semibold">已绑定身份</h3><Badge>{identityRows.length}</Badge></div><QueryState pending={identities.isPending} error={identities.error} onRetry={() => void identities.refetch()} />{identities.data && !identityRows.length ? <EmptyState icon={Fingerprint} compact title="没有已绑定身份" description="绑定企业身份后可使用 SSO 登录当前账号。" /> : <div className="divide-y divide-border">{identityRows.map((row) => <div key={textValue(row.identity_id, textValue(row.id))} className="flex items-center gap-3 px-4 py-3"><span className="flex size-8 items-center justify-center rounded-[5px] bg-surface-subtle"><Fingerprint className="size-4" /></span><div className="min-w-0 flex-1"><p className="truncate text-[13px] font-medium">{textValue(row.email, textValue(row.subject))}</p><p className="truncate font-mono text-[10px] text-muted-foreground">{textValue(row.issuer)}</p></div><Button variant="ghost" size="compact" onClick={() => unlink.mutate(textValue(row.identity_id, textValue(row.id, "")))}><Unlink className="size-3.5" />解除绑定</Button></div>)}</div>}</section> : null}
        <section className="border border-border bg-surface"><div className="flex h-11 items-center justify-between border-b border-border px-4"><div><h3 className="text-[13px] font-semibold">SCIM 自动预配</h3><p className="text-[11px] text-muted-foreground">服务提供方能力与工作区注册状态</p></div><StatusBadge status={config.data?.scim_enabled ? textValue(scimRow.status, "active") : "disabled"} /></div><div className="grid gap-px bg-border sm:grid-cols-3">{[["部署能力", config.data?.scim_enabled ? "已启用" : "未启用"], ["工作区状态", textValue(scimRow.status, "—")], ["资源端点", textValue(scimRow.base_url, "由服务端提供")]].map(([label, value]) => <div key={String(label)} className="bg-surface p-4"><p className="text-[11px] text-muted-foreground">{label}</p><p className="mt-1 text-[13px] font-medium">{String(value)}</p></div>)}</div>{config.data?.scim_enabled && typeof scimRow.base_url === "string" ? <div className="border-t border-border px-4 py-3"><Button asChild variant="ghost" size="compact"><a href={scimRow.base_url} target="_blank" rel="noreferrer">查看 SCIM 端点<ExternalLink className="size-3.5" /></a></Button></div> : null}</section>
      </div>}
    </AdminPageFrame>
  );
}
