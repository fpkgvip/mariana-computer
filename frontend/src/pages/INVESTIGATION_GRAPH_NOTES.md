# InvestigationGraph.tsx — Build Notes

## File
`/home/user/workspace/mariana/frontend/src/pages/InvestigationGraph.tsx` (1835 lines)

## Status
- TypeScript: ✅ zero errors with project tsconfig
- Vite build: ✅ succeeds
- D3 v7 + @types/d3: ✅ already in package.json

## Route Registration Needed
The file is NOT yet added to App.tsx routes. To integrate:

```tsx
import InvestigationGraph from "./pages/InvestigationGraph";

// Add these routes:
<Route path="/graph" element={<InvestigationGraph />} />
<Route path="/graph/:taskId" element={<InvestigationGraph />} />
```

## Features Implemented
1. **D3 v7 force-directed graph** — zoom/pan SVG canvas, light theme background (#FAF8F5)
2. **8 node types** — person, organization, event, document, claim, url, financial, data_point with distinct colors and emoji icons
3. **Left toolbar** — Add (N), Link (L), Fit (F), Cluster (C), Import, Export, Save, AI Populate
4. **Top bar with breadcrumb** — "Mariana > Investigation Graph" with back link, search + type filter
5. **Detail sidebar (right)** — Shows selected node: label, type badge, notes, connections list, clickable navigation
6. **Context menu (right-click)** — Edit, Pin/Unpin, Delete with viewport bounds checking
7. **Edit modal** — Type, label, notes with Escape/Enter handlers on all inputs
8. **Edge creation modal** — Asks for relationship label when linking two nodes
9. **AI integration** — POST /api/investigations/{taskId}/graph + 10s polling when RUNNING
10. **Import/Export JSON** — Full graph state with positions
11. **Keyboard shortcuts** — N, L, F, C, Delete/Backspace, Escape priority chain; modifier key guards; input/modal guards
12. **Stats badge** — Bottom-right "X nodes · Y edges"
13. **Toast notifications** — Uses sonner

## Bug Mitigations
- e.preventDefault() before toggleAddPanel() on N press
- Modal-open guard blocks all shortcuts except Escape
- Modifier keys (Ctrl/Meta/Alt) block shortcuts
- Escape priority chain with early returns
- closeSidebar calls updateGraph(false) to avoid reheat
- Edit modal inputs have own Escape/Enter keydown handlers
- Context menu bounds-checks against viewport edges
- Cluster button uses clearTimeout deduplication
- Self-link prevention in addEdge
- textContent used for D3 text elements (not innerHTML) — prevents XSS
- editingNode captured before closeEditModal nullifies it
