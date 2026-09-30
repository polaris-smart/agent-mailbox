# Workbench design

The user checks several employees in a bright desktop workspace and needs to make a decision quickly. A light default gives readable document and task surfaces; a matching dark option respects the user's environment.

## Color

Restrained: cool near-white content, a second neutral sidebar surface, existing brand blue `#4a6fa5` for the main action and selection. Body ink `#1d2026`; secondary text is darkened to meet contrast requirements. Status labels combine words with shapes and use restrained semantic tints. No gradient or decorative saturation.

## Type

System sans-serif including PingFang SC and Microsoft YaHei. Body 14px, compact labels 12px, task titles 14px/600, section titles 16px/600, page heading 25px/650. Prose has a bounded line length. No external fonts.

## Layout

224px desktop sidebar, 64px header, content capped at 1440px. Sidebar contains project selection and project pages, with device management at the bottom. Overview has status-grouped task rows and a narrower project-context rail. Tasks and employees use dense rows. Detail panel is a native dialog visually aligned to the right edge. At narrow widths the sidebar becomes a keyboard-accessible drawer and overview content stacks.

## Components

One button and input vocabulary with default, hover, focus, active, disabled, loading, and error states. Empty states explain a concrete next action. Lists use separators; cards are reserved for grouping context or the onboarding flow. Data appears only from the real service. Forms preserve entered values after errors.

Device onboarding has two explicit roles: invite from the coordinator, join on the other computer. A project-scoped, single-use invitation appears only in a temporary dialog. The remote computer must map an existing local folder before connecting an installed employee. Pairing does not imply file synchronization or tested physical-network connectivity.

Real-time updates use the authenticated local NDJSON change stream. Revision changes coalesce into one refresh; heartbeat messages never trigger database reads. Open forms keep their input, and hidden pages defer rendering until visible. Broken connections reconnect the stream and offer a manual retry.

Employee lifecycle is separate from tool connection readiness. Paused and retired identities remain visible in project history but do not appear in assignment choices. Retirement requires a written reason and an explicit confirmation of stopped assignments, revoked shared access, retained history, and the need for a new identity to rejoin. Management history names the actual actor and records reasons; it does not claim tamper-proof auditing. Permission requests show their absolute expiry and are rechecked before an allow action without a countdown poll.

The schema 3-to-4 upgrade retains existing employee identities while adding lifecycle and management history. The UI reads the service's migrated state; it does not implement or advertise an automatic application updater.

## Motion

150–200ms feedback for controls and drawer/dialog transitions. Skeletons indicate loading; no orchestrated entrance sequence. Reduced-motion removes animations and smooth scrolling.

## Resources

Vanilla HTML/CSS/JavaScript with inline local SVG icons. No CDN, external font, frontend framework, tracking, or background browser dependency.
