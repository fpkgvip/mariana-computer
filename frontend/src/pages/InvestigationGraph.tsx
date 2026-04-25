import {
  useState,
  useEffect,
  useRef,
  useCallback,
  useMemo,
  type ReactNode,
  type MouseEvent as ReactMouseEvent,
  type KeyboardEvent as ReactKeyboardEvent,
  type ChangeEvent,
} from "react";
import { useAuth } from "@/contexts/AuthContext";
import { supabase } from "@/lib/supabase";
import { Link, Navigate, useNavigate, useParams, useSearchParams } from "react-router-dom";
import {
  Plus,
  Link as LinkIcon,
  Maximize2,
  Shuffle,
  Download,
  Upload,
  Search,
  X,
  Edit,
  Pin,
  PinOff,
  Trash2,
  Zap,
  Brain,
  ArrowLeft,
  ChevronRight,
  Save,
  Filter,
  User,
  Building2,
  CalendarDays,
  FileText,
  AlertTriangle,
  Globe,
  DollarSign,
  Database,
} from "lucide-react";
import { toast } from "sonner";
import * as d3 from "d3";

/* ================================================================== */
/*  Types                                                             */
/* ================================================================== */

type NodeType =
  | "person"
  | "organization"
  | "event"
  | "document"
  | "claim"
  | "url"
  | "financial"
  | "data_point";

interface GraphNode {
  id: string;
  label: string;
  type: NodeType;
  notes: string;
  /**
   * Backend graph APIs persist rich node text in `description`; keep it optional
   * here so we can normalize it into `notes` on read while preserving editor UX.
   */
  description?: string;
  metadata?: Record<string, unknown>;
  source?: string;
  x?: number;
  y?: number;
  fx?: number | null;
  fy?: number | null;
  vx?: number;
  vy?: number;
  /** index injected by D3 */
  index?: number;
}

interface GraphEdge {
  id: string;
  source: string | GraphNode;
  sourceId: string;
  targetId: string;
  target: string | GraphNode;
  label: string;
}

interface GraphData {
  nodes: GraphNode[];
  edges: GraphEdge[];
}

interface ContextMenuState {
  visible: boolean;
  x: number;
  y: number;
  nodeId: string | null;
}

/* ================================================================== */
/*  Constants                                                         */
/* ================================================================== */

const API_URL = import.meta.env.VITE_API_URL ?? "";

const NODE_COLORS: Record<NodeType, string> = {
  person: "#3B5998",
  organization: "#BFA071",
  event: "#9B4D6E",
  document: "#4A7C59",
  claim: "#C75C3A",
  url: "#6B5B95",
  financial: "#2E8B8B",
  data_point: "#5A5A5A",
};

const NODE_LABELS: Record<NodeType, string> = {
  person: "Person",
  organization: "Organization",
  event: "Event",
  document: "Document",
  claim: "Claim",
  url: "URL",
  financial: "Financial",
  data_point: "Data Point",
};

const NODE_ICONS: Record<NodeType, typeof User> = {
  person: User,
  organization: Building2,
  event: CalendarDays,
  document: FileText,
  claim: AlertTriangle,
  url: Globe,
  financial: DollarSign,
  data_point: Database,
};

const ALL_NODE_TYPES: NodeType[] = [
  "person",
  "organization",
  "event",
  "document",
  "claim",
  "url",
  "financial",
  "data_point",
];

const BACKEND_NODE_TYPE_MAP: Record<string, NodeType> = {
  finding: "claim",
  hypothesis: "claim",
  source: "url",
  branch: "data_point",
  entity: "document",
};

function normalizeNodeType(type: string | null | undefined): NodeType {
  const normalized = (type ?? "").trim().toLowerCase();
  if ((ALL_NODE_TYPES as string[]).includes(normalized)) {
    return normalized as NodeType;
  }
  return BACKEND_NODE_TYPE_MAP[normalized] ?? "document";
}

const NODE_ICON_GLYPHS: Record<NodeType, string> = {
  person: "\u{1F464}",
  organization: "\u{1F3E2}",
  event: "\u{1F4C5}",
  document: "\u{1F4C4}",
  claim: "\u26A0",
  url: "\u{1F310}",
  financial: "\u{1F4B2}",
  data_point: "\u{1F4CA}",
};

const NODE_RADIUS = 24;
const POLL_INTERVAL_MS = 10_000;

/* ================================================================== */
/*  Helpers                                                           */
/* ================================================================== */

// BUG-25 / BUG-R7-03: Prefer crypto.randomUUID() when available, but older
// browsers may not implement it. Fall back to a time+random id so graph editing
// still works instead of throwing at runtime on first add/link action.
function makeClientId(prefix: string): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

const uid = () => makeClientId("node");
const eid = () => makeClientId("edge");

async function getAccessToken(): Promise<string | null> {
  const { data } = await supabase.auth.getSession();
  return data.session?.access_token ?? null;
}

/** Resolve edge source/target to an id string regardless of D3 mutation. */
function resolveId(ref: string | GraphNode): string {
  return typeof ref === "string" ? ref : ref.id;
}

function normalizeGraphData(data: GraphData): GraphData {
  const nodes = (data.nodes ?? []).map((node) => ({
    ...node,
    type: normalizeNodeType(node.type),
    notes: node.notes ?? node.description ?? "",
  }));

  const edges = (data.edges ?? []).map((edge) => ({
    ...edge,
    sourceId: edge.sourceId ?? resolveId(edge.source),
    targetId: edge.targetId ?? resolveId(edge.target),
    source: edge.sourceId ?? resolveId(edge.source),
    target: edge.targetId ?? resolveId(edge.target),
  }));

  return { nodes, edges };
}

/* ================================================================== */
/*  (Demo data removed — graph is per-investigation only)              */
/* ================================================================== */

/* ================================================================== */
/*  Component                                                         */
/* ================================================================== */

export default function InvestigationGraph() {
  const { user } = useAuth();
  const navigate = useNavigate();
  const params = useParams<{ taskId?: string }>();
  const [searchParams] = useSearchParams();
  const taskId = params.taskId ?? searchParams.get("task") ?? null;

  /* ---- Graph state ---- */
  const [nodes, setNodes] = useState<GraphNode[]>([]);
  const [edges, setEdges] = useState<GraphEdge[]>([]);
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);

  /* ---- UI state ---- */
  const [searchTerm, setSearchTerm] = useState("");
  const [filterType, setFilterType] = useState<NodeType | "all">("all");
  const [addPanelOpen, setAddPanelOpen] = useState(false);
  const [linkMode, setLinkMode] = useState(false);
  const [linkSourceId, setLinkSourceId] = useState<string | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [contextMenu, setContextMenu] = useState<ContextMenuState>({
    visible: false,
    x: 0,
    y: 0,
    nodeId: null,
  });
  const [editModalOpen, setEditModalOpen] = useState(false);
  const [editingNode, setEditingNode] = useState<GraphNode | null>(null);
  const [edgeLabelModalOpen, setEdgeLabelModalOpen] = useState(false);
  const [pendingEdge, setPendingEdge] = useState<{ sourceId: string; targetId: string } | null>(null);
  const [edgeLabelInput, setEdgeLabelInput] = useState("");
  const [aiLoading, setAiLoading] = useState(false);
  const [investigationRunning, setInvestigationRunning] = useState(false);
  const [graphError, setGraphError] = useState<string | null>(null);
  const [graphStatusMessage, setGraphStatusMessage] = useState("");

  /* ---- Add panel form state ---- */
  const [newLabel, setNewLabel] = useState("");
  const [newType, setNewType] = useState<NodeType>("person");
  const [newNotes, setNewNotes] = useState("");

  /* ---- Edit modal form state ---- */
  const [editLabel, setEditLabel] = useState("");
  const [editType, setEditType] = useState<NodeType>("person");
  const [editNotes, setEditNotes] = useState("");

  /* ---- Refs ---- */
  const svgRef = useRef<SVGSVGElement | null>(null);
  const gRef = useRef<SVGGElement | null>(null);
  const simulationRef = useRef<d3.Simulation<GraphNode, GraphEdge> | null>(null);
  const zoomRef = useRef<d3.ZoomBehavior<SVGSVGElement, unknown> | null>(null);
  const clusterTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const isMountedRef = useRef(true);
  // BUG-R2A-05: Track definitive errors so polling can bail out
  const graphErrorRef = useRef<string | null>(null);
  const nodesRef = useRef<GraphNode[]>(nodes);
  const edgesRef = useRef<GraphEdge[]>(edges);
  // BUG-03: Drag behavior created once in init, stored in ref
  const dragBehaviorRef = useRef<d3.DragBehavior<SVGGElement, GraphNode, GraphNode | d3.SubjectPosition> | null>(null);
  // BUG-20: Ref so drag filter can read link mode without stale closure
  const linkModeRef = useRef(linkMode);
  // BUG-R2A-02: Ref so D3 click handler reads linkSourceId without stale closure
  const linkSourceIdRef = useRef<string | null>(linkSourceId);
  const contextMenuRef = useRef<HTMLDivElement | null>(null);
  // Track whether the initial auto-fit has been performed after data loads
  const initialFitDoneRef = useRef(false);

  // Keep refs in sync
  nodesRef.current = nodes;
  edgesRef.current = edges;
  linkModeRef.current = linkMode;
  linkSourceIdRef.current = linkSourceId;

  const selectedNode = useMemo(
    () => nodes.find((n) => n.id === selectedNodeId) ?? null,
    [nodes, selectedNodeId]
  );

  /* ================================================================ */
  /*  No-taskId guard: redirect to chat                               */
  /* ================================================================ */

  // If no taskId is provided, this graph has nothing to show.

  /* ================================================================ */
  /*  Auth guard: redirect unauthenticated users to /login            */
  /* ================================================================ */

  useEffect(() => {
    if (!user) {
      const timer = setTimeout(() => navigate("/login", { replace: true }), 500);
      return () => clearTimeout(timer);
    }
  }, [user, navigate]);

  // BUG-R2A-01: Flip isMountedRef on unmount to prevent state updates after unmount
  useEffect(() => {
    isMountedRef.current = true;
    return () => {
      isMountedRef.current = false;
    };
  }, []);

  /* ================================================================ */
  /*  Merge incoming graph data (from backend / AI)                   */
  /* ================================================================ */

  const mergeGraph = useCallback((incoming: GraphData): { addedNodes: number; addedEdges: number } => {
    // BUG-FIX-01: Compute counts synchronously from refs to avoid stale values
    const existingNodeIds = new Set(nodesRef.current.map((n) => n.id));
    const incomingNodes = (incoming.nodes ?? []).filter((n) => !existingNodeIds.has(n.id));
    const addedNodes = incomingNodes.length;

    const existingEdgeIds = new Set(edgesRef.current.map((e) => e.id));
    const existingPairs = new Set(
      edgesRef.current.map((e) => `${resolveId(e.source)}|${resolveId(e.target)}`)
    );
    const normalizedEdges = (incoming.edges ?? []).map((e) => ({
      ...e,
      sourceId: e.sourceId ?? resolveId(e.source),
      targetId: e.targetId ?? resolveId(e.target),
    }));
    const incomingEdges = normalizedEdges.filter(
      (e) => !existingEdgeIds.has(e.id) && !existingPairs.has(`${e.sourceId}|${e.targetId}`)
    );
    const addedEdges = incomingEdges.length;

    if (addedNodes > 0) {
      setNodes((prev) => [...prev, ...incomingNodes]);
    }
    if (addedEdges > 0) {
      setEdges((prev) => [...prev, ...incomingEdges]);
    }
    return { addedNodes, addedEdges };
  }, []);

  /* ================================================================ */
  /*  API helpers                                                     */
  /* ================================================================ */

  const fetchGraphFromBackend = useCallback(async () => {
    if (!taskId) return;
    const token = await getAccessToken();
    if (!token) return;
    try {
      const res = await fetch(`${API_URL}/api/investigations/${taskId}/graph`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!isMountedRef.current) return;
      if (!res.ok) {
        // BUG-FIX-04: Distinguish definitive errors from transient ones
        if (res.status === 404) {
          setGraphError("Task not found. It may have been deleted.");
          graphErrorRef.current = "Task not found. It may have been deleted.";
        } else if (res.status === 403) {
          setGraphError("You do not have access to this task.");
          graphErrorRef.current = "You do not have access to this task.";
        } else if (res.status === 401) {
          // Session expired — redirect to login
          navigate("/login");
        } else {
          // Server error — surface to user
          setGraphError(`Failed to load graph (HTTP ${res.status}). Retrying...`);
          graphErrorRef.current = `Failed to load graph (HTTP ${res.status}). Retrying...`;
        }
        return;
      }
      if (!isMountedRef.current) return;
      setGraphError(null);
      graphErrorRef.current = null;
      const data: GraphData = await res.json();
      if (!isMountedRef.current) return;
      const normalized = normalizeGraphData(data);

      // FE-HIGH-04 fix: Merge incoming backend data with existing local node
      // positions instead of replacing. This prevents the 10s poll from wiping
      // user's drag-and-drop position edits. Only overwrite data fields (label,
      // type, notes); preserve x/y/fx/fy from existing nodes.
      const existingPositions = new Map(
        nodesRef.current.map((n) => [n.id, { x: n.x, y: n.y, fx: n.fx, fy: n.fy }])
      );
      const mergedNodes = normalized.nodes.map((incoming) => {
        const existing = existingPositions.get(incoming.id);
        if (existing) {
          return {
            ...incoming,
            x: existing.x,
            y: existing.y,
            fx: existing.fx,
            fy: existing.fy,
          };
        }
        return incoming;
      });
      setNodes(mergedNodes);
      setEdges(normalized.edges);
    } catch {
      // Network hiccup — surface error but don't crash
      if (isMountedRef.current) {
        setGraphError("Network error loading graph. Retrying...");
        graphErrorRef.current = "Network error loading graph. Retrying...";
      }
    }
  }, [taskId, navigate]);

  const saveGraphToBackend = useCallback(async () => {
    if (!taskId) {
      toast.info("No task linked — graph is local only");
      return;
    }
    const token = await getAccessToken();
    if (!token) {
      toast.error("Not authenticated");
      return;
    }
    try {
      const body: GraphData = {
        nodes: nodesRef.current.map((n) => ({
          id: n.id,
          label: n.label,
          type: n.type,
          notes: n.notes,
          description: n.notes,
          x: n.x,
          y: n.y,
          fx: n.fx ?? null,
          fy: n.fy ?? null,
        })),
        edges: edgesRef.current.map((e) => ({
          id: e.id,
          source: resolveId(e.source),
          sourceId: e.sourceId,
          target: resolveId(e.target),
          targetId: e.targetId,
          label: e.label,
        })),
      };
      const res = await fetch(`${API_URL}/api/investigations/${taskId}/graph`, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify(body),
      });
      if (res.ok) {
        toast.success("Graph saved to task");
      } else {
        toast.error("Failed to save graph");
      }
    } catch {
      toast.error("Network error while saving");
    }
  }, [taskId]);

  const aiPopulate = useCallback(async () => {
    if (!taskId) {
      toast.info("AI Populate requires an active task");
      return;
    }
    const token = await getAccessToken();
    if (!token) {
      toast.error("Not authenticated");
      return;
    }
    setAiLoading(true);
    try {
      const body: GraphData = {
        nodes: nodesRef.current.map((n) => ({
          id: n.id,
          label: n.label,
          type: n.type,
          notes: n.notes,
          description: n.notes,
        })),
        edges: edgesRef.current.map((e) => ({
          id: e.id,
          source: resolveId(e.source),
          sourceId: e.sourceId,
          target: resolveId(e.target),
          targetId: e.targetId,
          label: e.label,
        })),
      };
      const res = await fetch(`${API_URL}/api/investigations/${taskId}/graph`, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify(body),
      });
      if (!isMountedRef.current) return;
      if (!res.ok) {
        toast.error("AI populate request failed");
        return;
      }
      const data: GraphData = await res.json();
      const normalized = normalizeGraphData(data);
      // BUG-30: aiPopulate guard allows either nodes or edges (AI may return only one)
      if (normalized.nodes || normalized.edges) {
        const { addedNodes, addedEdges } = mergeGraph(normalized);
        toast.success(
          `AI added ${addedNodes} nodes and ${addedEdges} edges`
        );
      }
    } catch {
      toast.error("Network error during AI populate");
    } finally {
      setAiLoading(false);
    }
  }, [taskId, mergeGraph]);

  /* ================================================================ */
  /*  Poll backend when investigation is running                      */
  /* ================================================================ */

  useEffect(() => {
    setNodes([]);
    setEdges([]);
    setSelectedNodeId(null);
    setSidebarOpen(false);
    setSearchTerm("");
    setFilterType("all");
    setLinkMode(false);
    initialFitDoneRef.current = false;
    setLinkSourceId(null);
    setContextMenu({ visible: false, x: 0, y: 0, nodeId: null });
    setGraphError(null);
    graphErrorRef.current = null;
    setGraphStatusMessage(taskId ? `Loading graph for task ${taskId.slice(0, 8)}.` : "");
    setInvestigationRunning(false);
    if (!taskId) return;
    // Initial fetch
    fetchGraphFromBackend();

    // Check investigation status
    let cancelled = false;
    const checkStatus = async () => {
      const token = await getAccessToken();
      if (!token || cancelled) {
        if (!token && isMountedRef.current) navigate("/login");
        return;
      }
      try {
        const res = await fetch(`${API_URL}/api/investigations/${taskId}`, {
          headers: { Authorization: `Bearer ${token}` },
        });
        if (!isMountedRef.current || cancelled) return;
        if (res.status === 401) {
          navigate("/login");
          return;
        }
        if (!res.ok) return; // transient error — retry next cycle
        const data = await res.json();
        setGraphStatusMessage(`Investigation status: ${String(data.status ?? "unknown")}`);
        // BUG-F2-06: data.status is the canonical InvestigationStatus field
        const status = (data.status as string) ?? "";
        setInvestigationRunning(status === "RUNNING" || status === "PENDING");
      } catch {
        // Network hiccup — retry next cycle
      }
    };
    checkStatus();

    const interval = setInterval(() => {
      if (!cancelled) {
        // Only stop polling on definitive terminal errors (404/403).
        // Transient errors (5xx, network) contain "Retrying" and should not block.
        const err = graphErrorRef.current;
        if (err && !err.includes("Retrying")) return;
        fetchGraphFromBackend();
        checkStatus();
      }
    }, POLL_INTERVAL_MS);

    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, [taskId, fetchGraphFromBackend]);

  /* ================================================================ */
  /*  D3 Force Simulation + Rendering                                 */
  /* ================================================================ */

  // BUG-01: Tick handler registered once in init. BUG-03: drag created once in init.
  const updateGraph = useCallback(
    (reheat = true) => {
      const svg = svgRef.current;
      const g = gRef.current;
      if (!svg || !g) return;

      const container = d3.select(g);

      // ----- Edges -----
      const linkSel = container
        .selectAll<SVGGElement, GraphEdge>("g.edge-group")
        .data(edges, (d: GraphEdge) => d.id);

      linkSel.exit().transition().duration(300).style("opacity", 0).remove();

      const linkEnter = linkSel
        .enter()
        .append("g")
        .attr("class", "edge-group")
        .style("opacity", 0);

      linkEnter.append("line").attr("class", "edge-line");
      linkEnter.append("text").attr("class", "edge-label");

      linkEnter.transition().duration(500).style("opacity", 1);

      const linkMerge = linkEnter.merge(linkSel);

      linkMerge
        .select("line.edge-line")
        .attr("stroke", "#C4C4C4")
        .attr("stroke-width", 1.5)
        .attr("stroke-opacity", 0.6);

      linkMerge
        .select("text.edge-label")
        .text((d) => d.label)
        .attr("text-anchor", "middle")
        .attr("dy", -6)
        .attr("fill", "#6B7280")
        .attr("font-size", "11px")
        .attr("font-family", "Inter, system-ui, sans-serif")
        .attr("pointer-events", "none");

      // ----- Nodes -----
      const nodeSel = container
        .selectAll<SVGGElement, GraphNode>("g.node-group")
        .data(nodes, (d: GraphNode) => d.id);

      nodeSel.exit().transition().duration(300).style("opacity", 0).remove();

      const nodeEnter = nodeSel
        .enter()
        .append("g")
        .attr("class", "node-group")
        .style("opacity", 0)
        .style("cursor", "pointer");

      nodeEnter
        .append("circle")
        .attr("r", NODE_RADIUS)
        .attr("stroke", "#fff")
        .attr("stroke-width", 2.5);

      // Icon text (Unicode fallback – simpler than embedding SVG icons in D3)
      nodeEnter
        .append("text")
        .attr("class", "node-icon")
        .attr("text-anchor", "middle")
        .attr("dy", "0.35em")
        .attr("fill", "#fff")
        .attr("font-size", "14px")
        .attr("pointer-events", "none");

      nodeEnter
        .append("text")
        .attr("class", "node-label")
        .attr("text-anchor", "middle")
        .attr("dy", NODE_RADIUS + 16)
        .attr("font-size", "12px")
        .attr("font-family", "Inter, system-ui, sans-serif")
        .attr("pointer-events", "none");

      nodeEnter.transition().duration(500).style("opacity", 1);

      const nodeMerge = nodeEnter.merge(nodeSel);

      nodeMerge
        .select("circle")
        .attr("fill", (d) => NODE_COLORS[d.type])
        .attr("stroke", (d) => (d.id === selectedNodeId ? "#1B2036" : "#fff"))
        .attr("stroke-width", (d) => (d.id === selectedNodeId ? 3.5 : 2.5));

      nodeMerge.select("text.node-icon").text((d) => NODE_ICON_GLYPHS[d.type] ?? "?");

      nodeMerge
        .select("text.node-label")
        .text((d) => (d.label.length > 20 ? d.label.slice(0, 18) + "…" : d.label))
        .attr("fill", "#1B2036");

      /* -- Highlight matched nodes during search -- */
      const term = searchTerm.toLowerCase().trim();
      const typeFilter = filterType;
      nodeMerge.style("opacity", (d) => {
        if (typeFilter !== "all" && d.type !== typeFilter) return 0.15;
        if (term && !d.label.toLowerCase().includes(term)) return 0.15;
        return 1;
      });

      // BUG-03/20: Apply drag from ref; filter disables drag in link mode
      if (dragBehaviorRef.current) {
        nodeMerge.call(dragBehaviorRef.current);
      }

      // BUG-29: Typed as MouseEvent (not any)
      // BUG-R2A-02: Use linkSourceIdRef.current to avoid stale closure
      nodeMerge.on("click", (event: MouseEvent, d: GraphNode) => {
        if (linkModeRef.current && linkSourceIdRef.current) {
          // Complete link
          if (d.id === linkSourceIdRef.current) {
            toast.error("Cannot link a node to itself");
            return;
          }
          setPendingEdge({ sourceId: linkSourceIdRef.current, targetId: d.id });
          setEdgeLabelInput("");
          setEdgeLabelModalOpen(true);
          setLinkMode(false);
          setLinkSourceId(null);
        } else if (linkModeRef.current) {
          // Set as source
          setLinkSourceId(d.id);
          toast.info(`Now click a second node to create the link`);
        } else {
          setSelectedNodeId(d.id);
          setSidebarOpen(true);
        }
      });

      // BUG-29/37: Typed as MouseEvent; use actual DOM measurement for bounds
      nodeMerge.on("contextmenu", (event: MouseEvent, d: GraphNode) => {
        event.preventDefault();
        event.stopPropagation();
        const svgRect = svg.getBoundingClientRect();
        let cx = event.clientX - svgRect.left;
        let cy = event.clientY - svgRect.top;
        const menuWidth = contextMenuRef.current?.offsetWidth ?? 185;
        const menuHeight = contextMenuRef.current?.offsetHeight ?? 145;
        if (cx + menuWidth > svgRect.width) cx = svgRect.width - menuWidth - 5;
        if (cy + menuHeight > svgRect.height) cy = svgRect.height - menuHeight - 5;
        if (cx < 0) cx = 5;
        if (cy < 0) cy = 5;
        setContextMenu({ visible: true, x: cx, y: cy, nodeId: d.id });
      });

      // ----- Update simulation -----
      const sim = simulationRef.current;
      if (!sim) return;

      // BUG-04: sim.nodes() mutates objects in place; tick reads live positions
      sim.nodes(nodes);

      const simLinks = edges.map((e) => ({ ...e, source: resolveId(e.source), target: resolveId(e.target) }));
      const linkForce = sim.force("link") as d3.ForceLink<GraphNode, GraphEdge> | undefined;
      if (linkForce) linkForce.links(simLinks);
      if (reheat) sim.alpha(0.5).restart();
    },
    [nodes, edges, selectedNodeId, searchTerm, filterType]
  );

  /* ---- Initialize D3 simulation ---- */
  useEffect(() => {
    const svg = svgRef.current;
    if (!svg) return;

    const width = svg.clientWidth || 1200;
    const height = svg.clientHeight || 800;

    // BUG-02/08: Remove stale listeners before re-attaching (Strict Mode / HMR)
    d3.select(svg).on(".zoom", null);
    d3.select(svg).on("click", null);

    // Create zoom behavior
    const zoom = d3
      .zoom<SVGSVGElement, unknown>()
      .scaleExtent([0.1, 4])
      .on("zoom", (event) => {
        if (gRef.current) {
          d3.select(gRef.current).attr("transform", event.transform);
        }
      });

    d3.select(svg).call(zoom);
    zoomRef.current = zoom;

    // Create simulation
    const simulation = d3
      .forceSimulation<GraphNode>()
      .force(
        "link",
        d3
          .forceLink<GraphNode, GraphEdge>()
          .id((d) => d.id)
          .distance(100)
      )
      .force("charge", d3.forceManyBody().strength(-120))
      .force("x", d3.forceX(width / 2).strength(0.05))
      .force("y", d3.forceY(height / 2).strength(0.05))
      .force("collision", d3.forceCollide().radius(NODE_RADIUS + 6));

    simulationRef.current = simulation;

    // BUG-03: drag created once; BUG-05: end releases non-pinned; BUG-20: filter
    const wasPinned = new Map<string, boolean>();
    const drag = d3
      .drag<SVGGElement, GraphNode>()
      .filter((event) => !event.button && !linkModeRef.current)
      .clickDistance(5)
      .on("start", (event, d) => {
        if (!event.active) simulation.alphaTarget(0.3).restart();
        wasPinned.set(d.id, d.fx != null);
        d.fx = d.x;
        d.fy = d.y;
      })
      .on("drag", (event, d) => {
        d.fx = event.x;
        d.fy = event.y;
      })
      .on("end", (event, d) => {
        if (!event.active) simulation.alphaTarget(0);
        if (!wasPinned.get(d.id)) {
          d.fx = null;
          d.fy = null;
        }
        wasPinned.delete(d.id);
      });

    dragBehaviorRef.current = drag;

    // BUG-01: Register tick ONCE; BUG-04: read live positions from sim nodes
    // BUG-R2A-03: Guard against stale simulation firing after re-init
    simulation.on("tick", () => {
      if (simulationRef.current !== simulation) return;
      const g = gRef.current;
      if (!g) return;
      const gSel = d3.select(g);
      const simNodes = simulation.nodes();
      const nodeMap = new Map<string, GraphNode>();
      for (const n of simNodes) nodeMap.set(n.id, n);
      gSel.selectAll<SVGGElement, GraphEdge>("g.edge-group").each(function (d) {
        const src = nodeMap.get(d.sourceId);
        const tgt = nodeMap.get(d.targetId);
        const sx = src?.x ?? 0, sy = src?.y ?? 0;
        const tx = tgt?.x ?? 0, ty = tgt?.y ?? 0;
        const el = d3.select(this);
        el.select("line.edge-line").attr("x1", sx).attr("y1", sy).attr("x2", tx).attr("y2", ty);
        el.select("text.edge-label").attr("x", (sx + tx) / 2).attr("y", (sy + ty) / 2);
      });

      gSel
        .selectAll<SVGGElement, GraphNode>("g.node-group")
        .attr("transform", (d) => `translate(${d.x ?? 0},${d.y ?? 0})`);
    });

    // Note: Initial auto-fit is handled by a separate effect that watches
    // `nodes` for the first non-empty data load.  The simulation starts with
    // 0 nodes and would converge instantly, so calling fitGraph here would be
    // a no-op that falsely sets the "done" flag.

    // Click on background to deselect / close context menu
    d3.select(svg).on("click", () => {
      setContextMenu((prev) => (prev.visible ? { ...prev, visible: false } : prev));
    });

    return () => {
      simulation.on("tick", null); // BUG-R2A-03: detach tick before stopping
      simulation.on("end", null);
      simulation.stop();
      d3.select(svg).on(".zoom", null); // BUG-02/08
      d3.select(svg).on("click", null);
    };
  }, []);

  useEffect(() => { // BUG-26: clear cluster timeout on unmount
    return () => { if (clusterTimeoutRef.current) clearTimeout(clusterTimeoutRef.current); };
  }, []);

  // BUG-35: Single effect; reheat only when graph data changes, not on search/filter
  const prevNodesRef = useRef<GraphNode[]>(nodes);
  const prevEdgesRef = useRef<GraphEdge[]>(edges);
  useEffect(() => {
    const reheat = nodes !== prevNodesRef.current || edges !== prevEdgesRef.current;
    prevNodesRef.current = nodes;
    prevEdgesRef.current = edges;
    updateGraph(reheat);
  }, [updateGraph, nodes, edges, searchTerm, filterType, selectedNodeId]);

  /* ================================================================ */
  /*  Graph Mutations                                                 */
  /* ================================================================ */

  const addNode = useCallback(
    (label: string, type: NodeType, notes: string) => {
      if (!label.trim()) {
        toast.error("Label is required");
        return;
      }
      const node: GraphNode = {
        id: uid(),
        label: label.trim(),
        type,
        notes: notes.trim(),
        x: (svgRef.current?.clientWidth ?? 800) / 2 + (Math.random() - 0.5) * 100,
        y: (svgRef.current?.clientHeight ?? 600) / 2 + (Math.random() - 0.5) * 100,
      };
      setNodes((prev) => [...prev, node]);
      toast.success(`Added "${node.label}"`);
    },
    []
  );

  const deleteNode = useCallback(
    (nodeId: string) => {
      setNodes((prev) => prev.filter((n) => n.id !== nodeId));
      setEdges((prev) =>
        prev.filter((e) => resolveId(e.source) !== nodeId && resolveId(e.target) !== nodeId)
      );
      if (selectedNodeId === nodeId) {
        setSelectedNodeId(null);
        setSidebarOpen(false);
      }
      setContextMenu({ visible: false, x: 0, y: 0, nodeId: null });
      toast.success("Node deleted");
    },
    [selectedNodeId]
  );

  const addEdge = useCallback(
    (sourceId: string, targetId: string, label: string): boolean => {
      if (sourceId === targetId) {
        toast.error("Cannot link a node to itself");
        return false;
      }
      // BUG-10: No duplicate edges
      const alreadyLinked = edgesRef.current.some(
        (e) =>
          (resolveId(e.source) === sourceId && resolveId(e.target) === targetId) ||
          (resolveId(e.source) === targetId && resolveId(e.target) === sourceId)
      );
      if (alreadyLinked) { toast.error("These nodes are already linked"); return false; }
      const edge: GraphEdge = {
        id: eid(),
        source: sourceId,
        sourceId,
        target: targetId,
        targetId,
        label: label.trim() || "related to",
      };
      setEdges((prev) => [...prev, edge]);
      return true;
    },
    []
  );

  // BUG-12: pin/unpin mutate D3 sim node AND React state
  const pinNode = useCallback((nodeId: string) => {
    const simNode = simulationRef.current?.nodes().find((n) => n.id === nodeId);
    if (simNode) { simNode.fx = simNode.x ?? 0; simNode.fy = simNode.y ?? 0; }
    setNodes((prev) => prev.map((n) =>
      n.id !== nodeId ? n : { ...n, fx: simNode?.fx ?? n.x ?? 0, fy: simNode?.fy ?? n.y ?? 0 }
    ));
    setContextMenu({ visible: false, x: 0, y: 0, nodeId: null });
    toast.success("Node pinned");
  }, []);

  const unpinNode = useCallback((nodeId: string) => {
    const simNode = simulationRef.current?.nodes().find((n) => n.id === nodeId);
    if (simNode) { simNode.fx = null; simNode.fy = null; }
    setNodes((prev) => prev.map((n) =>
      n.id !== nodeId ? n : { ...n, fx: null, fy: null }
    ));
    setContextMenu({ visible: false, x: 0, y: 0, nodeId: null });
    toast.success("Node unpinned");
  }, []);

  const saveEditedNode = useCallback(() => {
    // Capture reference before closing modal
    const nodeToEdit = editingNode;
    if (!nodeToEdit) return;
    if (!editLabel.trim()) {
      toast.error("Label is required");
      return;
    }
    setNodes((prev) =>
      prev.map((n) =>
        n.id === nodeToEdit.id
          ? { ...n, label: editLabel.trim(), type: editType, notes: editNotes.trim() }
          : n
      )
    );
    setEditModalOpen(false);
    setEditingNode(null);
    toast.success("Node updated");
  }, [editingNode, editLabel, editType, editNotes]);

  /* ================================================================ */
  /*  Fit / Cluster                                                   */
  /* ================================================================ */

  const fitGraph = useCallback(() => {
    const svg = svgRef.current;
    const zoom = zoomRef.current;
    if (!svg || !zoom) return;
    // BUG-06: Read from sim nodes (live x/y), not React state
    const simNodes = simulationRef.current?.nodes() ?? [];
    if (simNodes.length === 0) return;

    const width = svg.clientWidth || 1200;
    const height = svg.clientHeight || 800;

    let minX = Infinity,
      maxX = -Infinity,
      minY = Infinity,
      maxY = -Infinity;
    for (const n of simNodes) {
      const x = n.x ?? 0;
      const y = n.y ?? 0;
      if (x < minX) minX = x;
      if (x > maxX) maxX = x;
      if (y < minY) minY = y;
      if (y > maxY) maxY = y;
    }

    const pad = 80;
    const dx = maxX - minX + pad * 2;
    const dy = maxY - minY + pad * 2;
    // Scale to fit all nodes, clamped to [0.02, 2].
    // For large graphs (92+ nodes), the bounding box can be 30k+ units,
    // requiring very low zoom.  We allow it — users can zoom in for details.
    const scale = Math.max(0.02, Math.min(width / dx, height / dy, 2));
    const cx = (minX + maxX) / 2;
    const cy = (minY + maxY) / 2;

    const transform = d3.zoomIdentity
      .translate(width / 2, height / 2)
      .scale(scale)
      .translate(-cx, -cy);

    d3.select(svg).transition().duration(750).call(zoom.transform, transform);
  }, []);

  // Auto-fit the viewport when graph data first arrives.
  // We wait 800ms after the first non-empty `nodes` render so the D3
  // simulation has time to assign reasonable x/y positions.  A second
  // fit at 2s catches the case where the simulation hasn't converged yet
  // but positions are close enough for the user to see the graph.
  useEffect(() => {
    if (initialFitDoneRef.current) return;
    if (nodes.length === 0) return;
    initialFitDoneRef.current = true;

    const t1 = setTimeout(() => fitGraph(), 1500);
    const t2 = setTimeout(() => fitGraph(), 4000);
    return () => { clearTimeout(t1); clearTimeout(t2); };
  }, [nodes.length, fitGraph]);

  const autoCluster = useCallback(() => {
    if (clusterTimeoutRef.current) clearTimeout(clusterTimeoutRef.current);
    const sim = simulationRef.current;
    if (!sim) return;
    // BUG-07: mutate sim nodes (live positions); preserve user pins
    const simNodes = sim.nodes();
    if (simNodes.length === 0) return;
    const userPinnedMap = new Map<string, { fx: number | null | undefined; fy: number | null | undefined }>();
    for (const n of simNodes) {
      if (n.fx != null) userPinnedMap.set(n.id, { fx: n.fx, fy: n.fy });
    }
    const groups: Record<string, GraphNode[]> = {};
    for (const n of simNodes) {
      if (!groups[n.type]) groups[n.type] = [];
      groups[n.type].push(n);
    }

    const typeKeys = Object.keys(groups);
    const cols = Math.ceil(Math.sqrt(typeKeys.length));
    const spacing = 300;

    typeKeys.forEach((type, i) => {
      const col = i % cols;
      const row = Math.floor(i / cols);
      const cx = col * spacing + spacing;
      const cy = row * spacing + spacing;
      groups[type].forEach((n, j) => {
        // Only temporarily pin non-user-pinned nodes for clustering
        if (!userPinnedMap.has(n.id)) {
          n.fx = cx + (j % 3) * 60 - 60;
          n.fy = cy + Math.floor(j / 3) * 60;
        }
      });
    });

    sim.alpha(0.8).restart();

    clusterTimeoutRef.current = setTimeout(() => {
      for (const n of sim.nodes()) {
        if (userPinnedMap.has(n.id)) {
          const saved = userPinnedMap.get(n.id)!;
          n.fx = saved.fx ?? null;
          n.fy = saved.fy ?? null;
        } else {
          n.fx = null;
          n.fy = null;
        }
      }
      setNodes((prev) => prev.map((n) => userPinnedMap.has(n.id) ? n : { ...n, fx: null, fy: null }));
      sim.alpha(0.3).restart();
    }, 2000);
  }, []);

  /* ================================================================ */
  /*  Import / Export                                                  */
  /* ================================================================ */

  const exportJSON = useCallback(() => {
    const data: GraphData = {
      nodes: nodesRef.current.map((n) => ({
        id: n.id,
        label: n.label,
        type: n.type,
        notes: n.notes,
        description: n.notes,
        x: n.x,
        y: n.y,
        fx: n.fx ?? null,
        fy: n.fy ?? null,
      })),
      edges: edgesRef.current.map((e) => ({
        id: e.id,
        source: resolveId(e.source),
        sourceId: e.sourceId,
        target: resolveId(e.target),
        targetId: e.targetId,
        label: e.label,
      })),
    };
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `investigation-graph-${taskId ?? "local"}-${Date.now()}.json`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(() => URL.revokeObjectURL(url), 100);
    toast.success("Graph exported");
  }, [taskId]);

  const importJSON = useCallback(
    (e: ChangeEvent<HTMLInputElement>) => {
      const file = e.target.files?.[0];
      if (!file) return;
      const reader = new FileReader();
      reader.onload = (ev) => {
        try {
          const data = JSON.parse(ev.target?.result as string) as GraphData;
          if (!Array.isArray(data.nodes) || !Array.isArray(data.edges)) {
            toast.error("Invalid graph JSON format");
            return;
          }
          // BUG-14: Validate node types and edge references
          const validNodes = (data.nodes as GraphNode[])
          .filter((n) => n.id && n.label)
          // BUG-F3-03: Normalize notes to empty string — imported JSON may omit this
          // field entirely. Without normalization, setEditNotes(node.notes) would
          // set a controlled textarea's value to undefined, producing a React
          // "switching from uncontrolled to controlled" warning and unexpected UI.
          .map((n) => ({
            ...n,
            type: normalizeNodeType(n.type),
            notes: n.notes ?? n.description ?? "",
          }));
          const nodeIds = new Set(validNodes.map((n) => n.id));
          // BUG-R3-03: Normalize sourceId/targetId (imported JSON may use source/target strings)
          const normalizedEdges = (data.edges as GraphEdge[]).map((e) => ({
            ...e,
            sourceId: e.sourceId ?? (typeof e.source === "string" ? e.source : ""),
            targetId: e.targetId ?? (typeof e.target === "string" ? e.target : ""),
          }));
          const validEdges = normalizedEdges.filter(
            (e) => e.id && nodeIds.has(e.sourceId) && nodeIds.has(e.targetId)
          );
          const skippedNodes = data.nodes.length - validNodes.length;
          const skippedEdges = data.edges.length - validEdges.length;
          if (skippedNodes > 0 || skippedEdges > 0) {
            toast.warning(`Skipped ${skippedNodes} invalid node(s) and ${skippedEdges} dangling edge(s)`);
          }
          setNodes(validNodes);
          setEdges(validEdges);
          // BUG-R2A-04: Stop any running simulation before D3 re-renders with new data
          if (simulationRef.current) {
            simulationRef.current.alpha(0).stop();
          }
          toast.success(`Imported ${validNodes.length} nodes and ${validEdges.length} edges`);
        } catch {
          toast.error("Failed to parse JSON file");
        }
      };
      reader.readAsText(file);
      // Reset input so same file can be re-imported
      e.target.value = "";
    },
    []
  );

  /* ================================================================ */
  /*  Toggle helpers                                                  */
  /* ================================================================ */

  const toggleAddPanel = useCallback(() => {
    setAddPanelOpen((prev) => {
      if (!prev) {
        // Opening — reset form
        setNewLabel("");
        // BUG-33: Also reset newType when opening the panel (consistent with label/notes)
        setNewType("person");
        setNewNotes("");
      }
      return !prev;
    });
  }, []);

  const toggleLinkMode = useCallback(() => {
    setLinkMode((prev) => {
      if (prev) {
        setLinkSourceId(null);
        toast.info("Link mode cancelled");
      } else {
        toast.info("Link mode: click source node, then target node");
      }
      return !prev;
    });
  }, []);

  // BUG-15: Removed stale setTimeout+updateGraph — state change triggers effect
  const closeSidebar = useCallback(() => {
    setSidebarOpen(false);
    setSelectedNodeId(null);
  }, []);

  const openEditModal = useCallback(
    (node: GraphNode) => {
      setEditingNode(node);
      setEditLabel(node.label);
      setEditType(node.type);
      setEditNotes(node.notes);
      setEditModalOpen(true);
      setContextMenu({ visible: false, x: 0, y: 0, nodeId: null });
    },
    []
  );

  const closeEditModal = useCallback(() => {
    setEditModalOpen(false);
    setEditingNode(null);
  }, []);

  /* ================================================================ */
  /*  Keyboard Shortcuts                                              */
  /* ================================================================ */

  useEffect(() => {
    const handler = (e: globalThis.KeyboardEvent) => {
      // Block all shortcuts when modifier keys held (except Escape)
      if ((e.ctrlKey || e.metaKey || e.altKey) && e.key !== "Escape") return;

      // Escape: close topmost layer (priority chain with early returns)
      if (e.key === "Escape") {
        if (editModalOpen) {
          e.preventDefault();
          closeEditModal();
          return;
        }
        if (edgeLabelModalOpen) {
          e.preventDefault();
          setEdgeLabelModalOpen(false);
          setPendingEdge(null);
          return;
        }
        if (contextMenu.visible) {
          e.preventDefault();
          setContextMenu({ visible: false, x: 0, y: 0, nodeId: null });
          return;
        }
        if (addPanelOpen) {
          e.preventDefault();
          setAddPanelOpen(false);
          return;
        }
        if (sidebarOpen) {
          e.preventDefault();
          closeSidebar();
          return;
        }
        if (linkMode) {
          e.preventDefault();
          setLinkMode(false);
          setLinkSourceId(null);
          toast.info("Link mode cancelled");
          return;
        }
        return;
      }

      // Don't trigger shortcuts when typing in inputs
      const tag = (e.target as HTMLElement).tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
      if ((e.target as HTMLElement).isContentEditable) return;

      // BUG-17/19: Guard includes addPanelOpen
      if (editModalOpen || edgeLabelModalOpen || addPanelOpen) return;

      switch (e.key.toLowerCase()) {
        case "n":
          e.preventDefault();
          toggleAddPanel();
          break;
        case "l":
          e.preventDefault();
          toggleLinkMode();
          break;
        case "f":
          e.preventDefault();
          fitGraph();
          break;
        case "c":
          e.preventDefault();
          autoCluster();
          break;
        case "delete":
        case "backspace":
          if (selectedNodeId) {
            e.preventDefault();
            deleteNode(selectedNodeId);
          }
          break;
      }
    };

    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [
    editModalOpen,
    edgeLabelModalOpen,
    contextMenu.visible,
    addPanelOpen,
    sidebarOpen,
    linkMode,
    selectedNodeId,
    toggleAddPanel,
    toggleLinkMode,
    fitGraph,
    autoCluster,
    deleteNode,
    closeEditModal,
    closeSidebar,
  ]);

  /* ---- Close context menu on outside click ---- */
  useEffect(() => {
    if (!contextMenu.visible) return;
    const handler = () => setContextMenu((prev) =>
      prev.visible ? { visible: false, x: 0, y: 0, nodeId: null } : prev
    );
    window.addEventListener("click", handler, { once: true });
    return () => window.removeEventListener("click", handler);
  }, [contextMenu.visible]);

  /* ================================================================ */
  /*  Connections for selected node                                   */
  /* ================================================================ */

  const selectedConnections = useMemo(() => {
    if (!selectedNodeId) return [];
    return edges
      .filter(
        (e) => resolveId(e.source) === selectedNodeId || resolveId(e.target) === selectedNodeId
      )
      .map((e) => {
        const otherId =
          resolveId(e.source) === selectedNodeId ? resolveId(e.target) : resolveId(e.source);
        const otherNode = nodes.find((n) => n.id === otherId);
        return { edge: e, otherNode, otherId };
      });
  }, [selectedNodeId, edges, nodes]);

  /* ================================================================ */
  /*  Render                                                          */
  /* ================================================================ */

  /* ================================================================ */
  /*  No-taskId: redirect to /chat                                    */
  /* ================================================================ */
  if (!taskId) {
    return <Navigate to="/chat" replace />;
  }

  return (
    <div className="flex flex-col h-screen bg-background overflow-hidden">
      {/* ============================================================ */}
      {/*  Top Bar / Breadcrumb                                       */}
      {/* ============================================================ */}
      <div className="flex items-center justify-between px-4 h-12 border-b border-border bg-card shrink-0 z-30">
        <div className="flex items-center gap-2 text-sm">
          <Link
            to="/chat"
            className="flex items-center gap-1 text-muted-foreground hover:text-foreground transition-colors"
          >
            <ArrowLeft className="w-4 h-4" />
            <span className="font-medium">Deft</span>
          </Link>
          <ChevronRight className="w-3.5 h-3.5 text-muted-foreground" />
          <span className="font-semibold text-foreground tracking-tight">
            Investigation Graph
          </span>
          {taskId && (
            <>
              <ChevronRight className="w-3.5 h-3.5 text-muted-foreground" />
              <span className="text-muted-foreground font-mono text-xs">{taskId.slice(0, 8)}</span>
              {investigationRunning && (
                <span className="ml-1.5 inline-flex items-center gap-1 px-2 py-0.5 text-xs font-medium rounded-full bg-blue-50 text-blue-700 ring-1 ring-blue-200">
                  <span className="w-1.5 h-1.5 rounded-full bg-blue-500 animate-pulse" />
                  Running
                </span>
              )}
            </>
          )}
        </div>

        {/* Search + Filter */}
        <div className="flex items-center gap-2">
          <div className="relative">
            <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-muted-foreground" />
            <input
              type="text"
              placeholder="Search nodes…"
              value={searchTerm}
              onChange={(e) => setSearchTerm(e.target.value)}
              className="pl-8 pr-3 py-1.5 text-sm border border-border rounded-md bg-background focus:outline-none focus:ring-2 focus:ring-ring/30 w-48"
              aria-label="Search graph nodes"
            />
            {searchTerm && (
              <button
                className="absolute right-2 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
                onClick={() => setSearchTerm("")}
                aria-label="Clear search"
              >
                <X className="w-3.5 h-3.5" />
              </button>
            )}
          </div>
          <div className="relative">
            <Filter className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-muted-foreground pointer-events-none" />
            <select
              value={filterType}
              onChange={(e) => setFilterType(e.target.value as NodeType | "all")}
              className="pl-8 pr-8 py-1.5 text-sm border border-border rounded-md bg-background appearance-none focus:outline-none focus:ring-2 focus:ring-ring/30"
              aria-label="Filter nodes by type"
            >
              <option value="all">All types</option>
              {ALL_NODE_TYPES.map((t) => (
                <option key={t} value={t}>
                  {NODE_LABELS[t]}
                </option>
              ))}
            </select>
          </div>
        </div>
      </div>

      {/* ============================================================ */}
      {/*  Main Content Area                                           */}
      {/* ============================================================ */}
      <div className="flex flex-1 overflow-hidden relative">
        {/* ========================================================== */}
        {/*  Left Toolbar                                              */}
        {/* ========================================================== */}
        <div className="flex flex-col items-center gap-1 py-3 px-1.5 border-r border-border bg-card shrink-0 z-20 w-12">
          <ToolbarButton
            icon={<Plus className="w-4 h-4" />}
            label="Add node (N)"
            active={addPanelOpen}
            onClick={toggleAddPanel}
          />
          <ToolbarButton
            icon={<LinkIcon className="w-4 h-4" />}
            label="Link nodes (L)"
            active={linkMode}
            onClick={toggleLinkMode}
          />
          <ToolbarButton
            icon={<Maximize2 className="w-4 h-4" />}
            label="Fit graph (F)"
            onClick={fitGraph}
          />
          <ToolbarButton
            icon={<Shuffle className="w-4 h-4" />}
            label="Auto-cluster (C)"
            onClick={autoCluster}
          />
          <div className="w-6 border-t border-border my-1" />
          <ToolbarButton
            icon={<Download className="w-4 h-4" />}
            label="Export JSON"
            onClick={exportJSON}
          />
          <ToolbarButton
            icon={<Upload className="w-4 h-4" />}
            label="Import JSON"
            onClick={() => fileInputRef.current?.click()}
          />
          <input
            ref={fileInputRef}
            type="file"
            accept=".json"
            className="hidden"
            onChange={importJSON}
          />
          {taskId && (
            <>
              <ToolbarButton
                icon={<Save className="w-4 h-4" />}
                label="Save to Investigation"
                onClick={saveGraphToBackend}
              />
              <div className="w-6 border-t border-border my-1" />
              <ToolbarButton
                icon={
                  aiLoading ? (
                    <Brain className="w-4 h-4 animate-pulse" />
                  ) : (
                    <Zap className="w-4 h-4" />
                  )
                }
                label="AI Populate"
                onClick={aiPopulate}
                accent
                disabled={aiLoading}
              />
            </>
          )}
        </div>

        {/* ========================================================== */}
        {/*  Add Node Panel (overlay on left)                          */}
        {/* ========================================================== */}
        {addPanelOpen && (
          <div
            className="absolute top-0 left-12 z-20 w-72 bg-card border-r border-b border-border shadow-lg rounded-br-lg p-4"
            role="dialog"
            aria-modal="true"
            aria-labelledby="add-node-title"
          >
            <div className="flex items-center justify-between mb-3">
              <h3
                id="add-node-title"
                className="text-sm font-semibold text-foreground"
                style={{ fontFamily: "'Playfair Display', Georgia, serif" }}
              >
                Add Node
              </h3>
              <button onClick={() => setAddPanelOpen(false)} className="text-muted-foreground hover:text-foreground" aria-label="Close add node panel">
                <X className="w-4 h-4" />
              </button>
            </div>
            <div className="space-y-3">
              <div>
                <label className="block text-xs font-medium text-muted-foreground mb-1">Label</label>
                <input
                  type="text"
                  value={newLabel}
                  onChange={(e) => setNewLabel(e.target.value)}
                  placeholder="Node label…"
                  className="w-full px-3 py-1.5 text-sm border border-border rounded-md bg-background focus:outline-none focus:ring-2 focus:ring-ring/30"
                  autoFocus
                  onKeyDown={(e: ReactKeyboardEvent<HTMLInputElement>) => {
                    if (e.key === "Enter" && newLabel.trim()) {
                      addNode(newLabel, newType, newNotes);
                      setNewLabel("");
                      setNewType("person");
                      setNewNotes("");
                    }
                    if (e.key === "Escape") {
                      setAddPanelOpen(false);
                    }
                  }}
                />
              </div>
              <div>
                <label className="block text-xs font-medium text-muted-foreground mb-1">Type</label>
                <select
                  value={newType}
                  onChange={(e) => setNewType(e.target.value as NodeType)}
                  className="w-full px-3 py-1.5 text-sm border border-border rounded-md bg-background appearance-none focus:outline-none focus:ring-2 focus:ring-ring/30"
                >
                  {ALL_NODE_TYPES.map((t) => (
                    <option key={t} value={t}>
                      {NODE_LABELS[t]}
                    </option>
                  ))}
                </select>
              </div>
              <div>
                <label className="block text-xs font-medium text-muted-foreground mb-1">Notes</label>
                <textarea
                  value={newNotes}
                  onChange={(e) => setNewNotes(e.target.value)}
                  placeholder="Optional notes…"
                  rows={2}
                  className="w-full px-3 py-1.5 text-sm border border-border rounded-md bg-background resize-none focus:outline-none focus:ring-2 focus:ring-ring/30"
                  onKeyDown={(e: ReactKeyboardEvent<HTMLTextAreaElement>) => {
                    if (e.key === "Escape") {
                      setAddPanelOpen(false);
                    }
                  }}
                />
              </div>
              <button
                onClick={() => {
                  addNode(newLabel, newType, newNotes);
                  setNewLabel("");
                  setNewType("person");
                  setNewNotes("");
                }}
                disabled={!newLabel.trim()}
                className="w-full py-1.5 text-sm font-medium rounded-md bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
              >
                Add Node
              </button>
            </div>
          </div>
        )}

        {/* ========================================================== */}
        {/*  SVG Canvas                                                */}
        {/* ========================================================== */}
        <div className="flex-1 relative overflow-hidden">
          <div className="sr-only" aria-live="polite" aria-atomic="true">
            {graphStatusMessage}
          </div>
          <svg
            ref={svgRef}
            className="w-full h-full"
            style={{ background: "#FAF8F5" }}
            role="img"
            aria-label="Investigation relationship graph"
          >
            <g ref={gRef} />
          </svg>

          {/* Link mode indicator */}
          {linkMode && (
            <div className="absolute top-3 left-1/2 -translate-x-1/2 z-10 flex items-center gap-2 px-4 py-2 bg-primary text-primary-foreground text-sm font-medium rounded-full shadow-lg">
              <LinkIcon className="w-4 h-4" />
              {linkSourceId ? "Click target node…" : "Click source node…"}
              <button
                onClick={() => {
                  setLinkMode(false);
                  setLinkSourceId(null);
                }}
                className="ml-1 hover:bg-white/20 rounded p-0.5"
                aria-label="Cancel link mode"
              >
                <X className="w-3.5 h-3.5" />
              </button>
            </div>
          )}

          {/* Context Menu */}
          {contextMenu.visible && contextMenu.nodeId && (
            <div
              ref={contextMenuRef}
              className="absolute z-30 bg-card border border-border rounded-lg shadow-xl py-1 min-w-[180px]"
              style={{ left: contextMenu.x, top: contextMenu.y }}
              onClick={(e: ReactMouseEvent) => e.stopPropagation()}
            >
              <ContextMenuItem
                icon={<Edit className="w-3.5 h-3.5" />}
                label="Edit node"
                onClick={() => {
                  const node = nodes.find((n) => n.id === contextMenu.nodeId);
                  if (node) openEditModal(node);
                }}
              />
              {(() => {
                const node = nodes.find((n) => n.id === contextMenu.nodeId);
                const isPinned = node?.fx != null;
                return (
                  <ContextMenuItem
                    icon={isPinned ? <PinOff className="w-3.5 h-3.5" /> : <Pin className="w-3.5 h-3.5" />}
                    label={isPinned ? "Unpin position" : "Pin position"}
                    onClick={() => {
                      if (isPinned) unpinNode(contextMenu.nodeId!);
                      else pinNode(contextMenu.nodeId!);
                    }}
                  />
                );
              })()}
              <div className="border-t border-border my-1" />
              <ContextMenuItem
                icon={<Trash2 className="w-3.5 h-3.5" />}
                label="Delete node"
                destructive
                onClick={() => deleteNode(contextMenu.nodeId!)}
              />
            </div>
          )}

          {/* Stats badge */}
          <div className="absolute bottom-3 right-3 z-10 flex items-center gap-1.5 px-3 py-1.5 bg-card/90 backdrop-blur border border-border rounded-full text-xs text-muted-foreground font-medium shadow-sm">
            <span>{nodes.length} node{nodes.length !== 1 ? "s" : ""}</span>
            <span>·</span>
            <span>{edges.length} edge{edges.length !== 1 ? "s" : ""}</span>
          </div>

          {/* Error state */}
          {graphError && (
            <div className="absolute inset-0 flex flex-col items-center justify-center pointer-events-none" role="alert" aria-live="assertive">
              <AlertTriangle className="w-12 h-12 text-red-400/60 mb-3" />
              <p className="text-red-400 text-sm font-medium mb-1">{graphError}</p>
              <Link
                to="/chat"
                className="pointer-events-auto text-xs text-muted-foreground underline underline-offset-2 hover:text-foreground transition-colors"
              >
                Return to tasks
              </Link>
            </div>
          )}

          {/* Empty state */}
          {nodes.length === 0 && !graphError && (
            <div className="absolute inset-0 flex flex-col items-center justify-center pointer-events-none">
              <Brain className="w-12 h-12 text-muted-foreground/30 mb-3" />
              <p className="text-muted-foreground text-sm">No nodes yet. Press N to add one.</p>
            </div>
          )}
        </div>

        {/* ========================================================== */}
        {/*  Detail Sidebar (right)                                    */}
        {/* ========================================================== */}
        {sidebarOpen && selectedNode && (
          <div className="w-80 border-l border-border bg-card shrink-0 flex flex-col z-20 overflow-hidden">
            {/* Header */}
            <div className="flex items-center justify-between px-4 py-3 border-b border-border">
              <h3
                className="text-sm font-semibold text-foreground truncate"
                style={{ fontFamily: "'Playfair Display', Georgia, serif" }}
              >
                Node Details
              </h3>
              <button onClick={closeSidebar} className="text-muted-foreground hover:text-foreground" aria-label="Close node details">
                <X className="w-4 h-4" />
              </button>
            </div>

            {/* Content */}
            <div className="flex-1 overflow-y-auto p-4 space-y-4">
              {/* Label + type badge */}
              <div>
                <p className="text-base font-semibold text-foreground mb-1.5">{selectedNode.label}</p>
                <span
                  className="inline-flex items-center gap-1.5 px-2.5 py-0.5 text-xs font-medium rounded-full text-white"
                  style={{ backgroundColor: NODE_COLORS[selectedNode.type] }}
                >
                  {(() => {
                    const Icon = NODE_ICONS[selectedNode.type];
                    return <Icon className="w-3 h-3" />;
                  })()}
                  {NODE_LABELS[selectedNode.type]}
                </span>
              </div>

              {/* Notes */}
              <div>
                <label className="block text-xs font-medium text-muted-foreground mb-1">Notes</label>
                <p className="text-sm text-foreground/80 leading-relaxed">
                  {selectedNode.notes || "No notes"}
                </p>
              </div>

              {/* Edit button */}
              <button
                onClick={() => openEditModal(selectedNode)}
                className="w-full flex items-center justify-center gap-1.5 py-1.5 text-sm font-medium rounded-md border border-border hover:bg-muted transition-colors"
              >
                <Edit className="w-3.5 h-3.5" />
                Edit Node
              </button>

              {/* Connections */}
              <div>
                <label className="block text-xs font-medium text-muted-foreground mb-2">
                  Connections ({selectedConnections.length})
                </label>
                {selectedConnections.length === 0 ? (
                  <p className="text-xs text-muted-foreground">No connections</p>
                ) : (
                  <div className="space-y-1.5">
                    {selectedConnections.map(({ edge, otherNode, otherId }) => (
                      <button
                        key={edge.id}
                        className="w-full flex items-center gap-2 px-2.5 py-2 rounded-md hover:bg-muted transition-colors text-left"
                        onClick={() => {
                          setSelectedNodeId(otherId);
                        }}
                      >
                        {otherNode && (
                          <span
                            className="w-2 h-2 rounded-full shrink-0"
                            style={{ backgroundColor: NODE_COLORS[otherNode.type] }}
                          />
                        )}
                        <div className="min-w-0 flex-1">
                          <p className="text-sm font-medium text-foreground truncate">
                            {otherNode?.label ?? otherId}
                          </p>
                          <p className="text-xs text-muted-foreground truncate">{edge.label}</p>
                        </div>
                      </button>
                    ))}
                  </div>
                )}
              </div>
            </div>
          </div>
        )}
      </div>

      {/* ============================================================ */}
      {/*  Edit Modal                                                  */}
      {/* ============================================================ */}
      {editModalOpen && editingNode && (
        <ModalOverlay onClose={closeEditModal}>
          <div
            className="bg-card rounded-xl border border-border shadow-2xl w-full max-w-md p-6"
            onClick={(e: ReactMouseEvent) => e.stopPropagation()}
            role="dialog"
            aria-modal="true"
            aria-labelledby="edit-node-title"
          >
            <h3
              id="edit-node-title"
              className="text-lg font-semibold text-foreground mb-4"
              style={{ fontFamily: "'Playfair Display', Georgia, serif" }}
            >
              Edit Node
            </h3>
            <div className="space-y-4">
              <div>
                <label className="block text-xs font-medium text-muted-foreground mb-1">Label</label>
                <input
                  type="text"
                  value={editLabel}
                  onChange={(e) => setEditLabel(e.target.value)}
                  className="w-full px-3 py-2 text-sm border border-border rounded-md bg-background focus:outline-none focus:ring-2 focus:ring-ring/30"
                  autoFocus
                  onKeyDown={(e: ReactKeyboardEvent<HTMLInputElement>) => {
                    if (e.key === "Enter") {
                      e.preventDefault();
                      saveEditedNode();
                    }
                    if (e.key === "Escape") {
                      e.preventDefault();
                      e.stopPropagation();
                      closeEditModal();
                    }
                  }}
                />
              </div>
              <div>
                <label className="block text-xs font-medium text-muted-foreground mb-1">Type</label>
                <select
                  value={editType}
                  onChange={(e) => setEditType(e.target.value as NodeType)}
                  className="w-full px-3 py-2 text-sm border border-border rounded-md bg-background appearance-none focus:outline-none focus:ring-2 focus:ring-ring/30"
                  onKeyDown={(e: ReactKeyboardEvent<HTMLSelectElement>) => {
                    if (e.key === "Escape") {
                      e.preventDefault();
                      e.stopPropagation();
                      closeEditModal();
                    }
                  }}
                >
                  {ALL_NODE_TYPES.map((t) => (
                    <option key={t} value={t}>
                      {NODE_LABELS[t]}
                    </option>
                  ))}
                </select>
              </div>
              <div>
                <label className="block text-xs font-medium text-muted-foreground mb-1">Notes</label>
                <textarea
                  value={editNotes}
                  onChange={(e) => setEditNotes(e.target.value)}
                  rows={3}
                  className="w-full px-3 py-2 text-sm border border-border rounded-md bg-background resize-none focus:outline-none focus:ring-2 focus:ring-ring/30"
                  onKeyDown={(e: ReactKeyboardEvent<HTMLTextAreaElement>) => {
                    if (e.key === "Escape") {
                      e.preventDefault();
                      e.stopPropagation();
                      closeEditModal();
                    }
                  }}
                />
              </div>
            </div>
            <div className="flex items-center justify-end gap-2 mt-5">
              <button
                onClick={closeEditModal}
                className="px-4 py-2 text-sm font-medium rounded-md border border-border hover:bg-muted transition-colors"
              >
                Cancel
              </button>
              <button
                onClick={saveEditedNode}
                disabled={!editLabel.trim()}
                className="px-4 py-2 text-sm font-medium rounded-md bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
              >
                Save
              </button>
            </div>
          </div>
        </ModalOverlay>
      )}

      {/* ============================================================ */}
      {/*  Edge Label Modal                                            */}
      {/* ============================================================ */}
      {edgeLabelModalOpen && pendingEdge && (
        <ModalOverlay
          onClose={() => {
            setEdgeLabelModalOpen(false);
            setPendingEdge(null);
          }}
        >
          <div
            className="bg-card rounded-xl border border-border shadow-2xl w-full max-w-sm p-6"
            onClick={(e: ReactMouseEvent) => e.stopPropagation()}
            role="dialog"
            aria-modal="true"
            aria-labelledby="create-link-title"
          >
            <h3
              id="create-link-title"
              className="text-lg font-semibold text-foreground mb-1"
              style={{ fontFamily: "'Playfair Display', Georgia, serif" }}
            >
              Create Link
            </h3>
            <p className="text-xs text-muted-foreground mb-4">
              {nodes.find((n) => n.id === pendingEdge.sourceId)?.label ?? "?"} →{" "}
              {nodes.find((n) => n.id === pendingEdge.targetId)?.label ?? "?"}
            </p>
            <div>
              <label className="block text-xs font-medium text-muted-foreground mb-1">
                Relationship label
              </label>
              <input
                type="text"
                value={edgeLabelInput}
                onChange={(e) => setEdgeLabelInput(e.target.value)}
                placeholder="e.g. reported to, controls, filed by…"
                className="w-full px-3 py-2 text-sm border border-border rounded-md bg-background focus:outline-none focus:ring-2 focus:ring-ring/30"
                autoFocus
                onKeyDown={(e: ReactKeyboardEvent<HTMLInputElement>) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    const ok = addEdge(pendingEdge.sourceId, pendingEdge.targetId, edgeLabelInput);
                    if (ok) {
                      setEdgeLabelModalOpen(false);
                      setPendingEdge(null);
                      toast.success("Link created");
                    }
                  }
                  if (e.key === "Escape") {
                    e.preventDefault();
                    e.stopPropagation();
                    setEdgeLabelModalOpen(false);
                    setPendingEdge(null);
                  }
                }}
              />
            </div>
            <div className="flex items-center justify-end gap-2 mt-5">
              <button
                onClick={() => {
                  setEdgeLabelModalOpen(false);
                  setPendingEdge(null);
                }}
                className="px-4 py-2 text-sm font-medium rounded-md border border-border hover:bg-muted transition-colors"
              >
                Cancel
              </button>
              <button
                onClick={() => {
                  const ok = addEdge(pendingEdge.sourceId, pendingEdge.targetId, edgeLabelInput);
                  if (ok) {
                    setEdgeLabelModalOpen(false);
                    setPendingEdge(null);
                    toast.success("Link created");
                  }
                }}
                className="px-4 py-2 text-sm font-medium rounded-md bg-primary text-primary-foreground hover:bg-primary/90 transition-colors"
              >
                Create Link
              </button>
            </div>
          </div>
        </ModalOverlay>
      )}
    </div>
  );
}

/* ================================================================== */
/*  Sub-components                                                    */
/* ================================================================== */

function ToolbarButton({
  icon,
  label,
  onClick,
  active = false,
  accent = false,
  disabled = false,
}: {
  icon: ReactNode;
  label: string;
  onClick: () => void;
  active?: boolean;
  accent?: boolean;
  disabled?: boolean;
}) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      title={label}
      aria-label={label}
      className={`
        w-9 h-9 flex items-center justify-center rounded-lg transition-colors
        ${disabled ? "opacity-40 cursor-not-allowed" : ""}
        ${
          active
            ? "bg-primary text-primary-foreground"
            : accent
            ? "bg-accent text-white hover:bg-accent/90"
            : "text-muted-foreground hover:text-foreground hover:bg-muted"
        }
      `}
    >
      {icon}
    </button>
  );
}

function ContextMenuItem({
  icon,
  label,
  onClick,
  destructive = false,
}: {
  icon: ReactNode;
  label: string;
  onClick: () => void;
  destructive?: boolean;
}) {
  return (
    <button
      onClick={onClick}
      className={`
        w-full flex items-center gap-2.5 px-3 py-2 text-sm transition-colors
        ${
          destructive
            ? "text-destructive hover:bg-destructive/10"
            : "text-foreground hover:bg-muted"
        }
      `}
    >
      {icon}
      <span>{label}</span>
    </button>
  );
}

function ModalOverlay({
  children,
  onClose,
}: {
  children: ReactNode;
  onClose: () => void;
}) {
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 backdrop-blur-sm"
      onClick={onClose}
    >
      {children}
    </div>
  );
}
