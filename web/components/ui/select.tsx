"use client";

import * as SelectPrimitive from "@radix-ui/react-select";
import { Check, ChevronDown } from "lucide-react";
import { cn } from "@/lib/utils";

export const Select = SelectPrimitive.Root;
export const SelectValue = SelectPrimitive.Value;

export function SelectTrigger({ className, children, ...props }: React.ComponentProps<typeof SelectPrimitive.Trigger>) {
  return (
    <SelectPrimitive.Trigger className={cn("flex h-9 items-center justify-between gap-2 rounded-[7px] border border-border-strong bg-surface px-2.5 text-[13px] outline-none transition-colors hover:bg-surface-subtle focus:border-primary/60 focus:ring-2 focus:ring-primary/10 disabled:cursor-not-allowed disabled:opacity-60", className)} {...props}>
      {children}<SelectPrimitive.Icon><ChevronDown className="size-3.5 text-muted-foreground" /></SelectPrimitive.Icon>
    </SelectPrimitive.Trigger>
  );
}

export function SelectContent({ className, children, position = "popper", ...props }: React.ComponentProps<typeof SelectPrimitive.Content>) {
  return (
    <SelectPrimitive.Portal>
      <SelectPrimitive.Content position={position} className={cn("z-50 min-w-[var(--radix-select-trigger-width)] rounded-[8px] border border-border bg-surface-raised p-1 shadow-[var(--shadow-float)]", className)} {...props}>
        <SelectPrimitive.Viewport>{children}</SelectPrimitive.Viewport>
      </SelectPrimitive.Content>
    </SelectPrimitive.Portal>
  );
}

export function SelectItem({ className, children, ...props }: React.ComponentProps<typeof SelectPrimitive.Item>) {
  return (
    <SelectPrimitive.Item className={cn("relative flex h-8 select-none items-center rounded-[6px] py-1 pl-8 pr-2 text-[13px] outline-none data-[highlighted]:bg-surface-subtle", className)} {...props}>
      <span className="absolute left-2 flex size-4 items-center justify-center"><SelectPrimitive.ItemIndicator><Check className="size-3.5" /></SelectPrimitive.ItemIndicator></span>
      <SelectPrimitive.ItemText>{children}</SelectPrimitive.ItemText>
    </SelectPrimitive.Item>
  );
}
