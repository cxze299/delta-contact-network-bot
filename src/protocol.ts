import { z } from "zod";
export const Command = z
  .object({
    token: z.string().min(32).max(100),
    id: z.string().regex(/^[a-f0-9]{32}$/),
    action: z.enum([
      "refresh",
      "start",
      "join_network",
      "cancel_join",
      "use_current_account",
      "directory",
      "command",
      "reply",
      "exchange_now",
      "auto_share",
      "admin_open",
      "admin_unlock",
      "admin_lock",
      "admin_select",
      "admin_action",
      "recover_by_name",
    ]),
    network: z.number().int().nullable().optional(),
    value: z.string().max(2000).optional(),
    notification_version: z.literal(1).optional(),
    nickname: z.string().max(40).optional(),
    consent: z.boolean().optional(),
    use_current_account: z.boolean().optional(),
    discoverable: z.boolean().optional(),
    accepts_requests: z.boolean().optional(),
    enabled: z.boolean().optional(),
    page: z.number().int().min(0).max(999).optional(),
    proof: z
      .string()
      .regex(/^[a-f0-9]{64}$/)
      .optional(),
    admin_network: z.number().int().positive().optional(),
    operation: z.string().max(40).optional(),
    data: z.record(z.string(), z.string().max(500)).optional(),
    step: z.string().max(100).optional(),
    command: z.string().max(40).optional(),
    verify_current_account: z.boolean().optional(),
    join_code: z.string().max(200).optional(),
    old_address: z.string().max(254).optional(),
  })
  .strict();
export type Command = z.infer<typeof Command>;
export class UserError extends Error {}
export const now = () => new Date().toISOString();
export const later = (days: number) =>
  new Date(Date.now() + days * 86400000).toISOString();
