# Tool catalog

The server registers 64 tools across four architectural layers. The default surface contains 43 read-only tools (34 core REST v1 tools, 3 AI Bookkeeping Inbox tools, and 6 web session inspection tools). Enabling writes exposes 21 write tools (14 accounting write tools, 1 archive tool, and 6 web session write tools), gated behind explicit confirmation and native OS biometrics.

## Search and batch detail

| Tool | Purpose |
|---|---|
| `search_relations` | Search relations with compact results and bounded scanning. |
| `search_mutations` | Search journal entries across practical filters. |
| `search_invoices` | Search invoices and optionally hydrate detail. |
| `get_relations_batch` | Fetch details for a bounded list of relation IDs. |
| `get_mutations_batch` | Fetch full mutation rows and VAT for multiple IDs. |
| `get_invoices_batch` | Fetch invoice line details for multiple IDs. |

## Reports

| Tool | Purpose |
|---|---|
| `get_trial_balance` | Aggregate debit, credit, and balance by ledger. |
| `get_profit_loss` | Build a profit-and-loss view for a period. |
| `get_balance_sheet` | Build a balance-sheet view at a date. |
| `get_vat_summary` | Summarize VAT amounts by VAT code. |
| `get_ar_ap_aging` | Age outstanding receivables or payables. |
| `get_ledger_transactions` | Return bounded transactions for one ledger. |

## Accounting records

| Area | Read tools | Opt-in write tools |
|---|---|---|
| Relations | `list_relations`, `get_relation` | `create_relation`, `update_relation`, `delete_relation` |
| Invoices | `list_invoices`, `get_invoice` | `create_invoice` |
| Mutations | `list_mutations`, `get_mutation`, `list_outstanding_invoices` | `create_mutation` |
| Products | `list_products`, `get_product`, `list_product_groups` | `create_product`, `update_product`, `delete_product` |
| Ledgers | `list_ledgers`, `get_ledger`, `get_ledger_balance` | `create_ledger`, `update_ledger` |
| Cost centers | `list_cost_centers`, `get_cost_center` | `create_cost_center`, `delete_cost_center` |
| Members | `list_members`, `get_member` | `create_member`, `delete_member` |
| Administrations | `list_administrations`, `list_linked_administrations` | - |
| Templates | `list_invoice_templates`, `list_email_templates` | - |
| Units | `list_units` | - |
| Digital archive | - | `send_file_to_digital_archive` |

## AI Bookkeeping Inbox & Heuristics

| Tool | Purpose | Read/Write |
|---|---|---|
| `get_dutch_bookkeeping_rules` | Return authoritative Dutch VAT rules, decision tree, receipt heuristics, and tax tips. | Read-only |
| `prepare_booking_proposal` | Validate extracted invoice data against live ledgers/relations to synthesize balanced bookings. | Read-only |
| `suggest_transaction_classification` | Classify bank statement lines and suggest ledger accounts, VAT codes, and mutation types. | Read-only |

## Web Session Bridge (Optional)

| Tool | Purpose | Read/Write |
|---|---|---|
| `get_unprocessed_bank_transactions` | Read raw bank statement entries directly from e-Boekhouden's internal import grid. | Read-only |
| `suggest_mutation_for_bank_statement` | Get mutation prefill suggestions from internal bank matching logic. | Read-only |
| `attach_file_to_mutation` | Link an uploaded archive document directly to a mutation via `/folderkoppel`. | Opt-in Write |
| `list_archive_folders` | List Digital Archive folders (including Mail & Herken inbox folders). | Read-only |
| `list_archive_files` | List files and Mail & Herken recognition status inside a Digital Archive folder. | Read-only |
| `get_recognized_invoice_model` | Retrieve internal AI OCR recognition model for a Digital Archive file. | Read-only |
| `get_mutation_model_by_number` | Retrieve editable web mutation model by mutation number (`nr`). | Read-only |
| `save_web_mutation` | Create or update a mutation using the normalized web mutation model. | Opt-in Write |
| `upload_archive_file` | Upload a local file from disk directly into a Digital Archive folder. | Opt-in Write |
| `move_archive_files` | Move Digital Archive files into a target folder. | Opt-in Write |
| `create_archive_folder` | Create a new folder in the Digital Archive. | Opt-in Write |
| `delete_web_mutations` | Resolve mutation numbers (`nr`) to internal grid IDs and delete them. | Opt-in Write |

## Result behavior

- Standard list tools follow the API's `limit` range of 1 through 2000 and a non-negative `offset`.
- Power searches and batch tools cap work to protect API usage and model context.
- Compact output removes null fields unless verbose output is requested.
- A truncated result includes a hint describing how to request the next slice.
- Decimal and date values are serialized as strings where needed for JSON.
- API failures return structured MCP errors rather than successful text results.

## Write behavior

Write tools are not advertised until `EBOEKHOUDEN_MCP_ENABLE_WRITE_TOOLS=true`. Every advertised write schema contains a required `confirm` boolean. The server accepts the operation only when its value is exactly `true`.

When `EBOEKHOUDEN_MCP_REQUIRE_BIOMETRIC=true` (the default), any write action also prompts for native OS biometric verification (Windows Hello / Touch ID) with an action summary before dispatching to the API.
