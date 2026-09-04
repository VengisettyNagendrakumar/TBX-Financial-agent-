"""
Ultra-Clear Professional Architecture Flow Diagram Generator
============================================================
Generates an impeccably organized, publication-grade enterprise architecture
flow diagram with zero overlapping lines, clear swimlane columns, explicit
numbered step badges, and a distinct safety guardrail loop.
"""

import matplotlib.pyplot as plt
import matplotlib.patches as patches

def draw_card(ax, x, y, w, h, title, subtitle, items, header_color, bg_color="#131C2E", border_color=None):
    """Draws a crisp modern card with an accent top band and high-readability items."""
    if border_color is None:
        border_color = header_color
        
    # Card Background & Border
    card = patches.FancyBboxPatch(
        (x, y), w, h, 
        boxstyle="round,pad=0.06,rounding_size=0.16",
        facecolor=bg_color, edgecolor=border_color, linewidth=2, zorder=2
    )
    ax.add_patch(card)
    
    # Header Accent Band
    header_h = 0.65
    header_band = patches.FancyBboxPatch(
        (x, y + h - header_h), w, header_h, 
        boxstyle="round,pad=0.06,rounding_size=0.16",
        facecolor=header_color, edgecolor="none", zorder=3
    )
    ax.add_patch(header_band)
    
    # Square off the bottom of the rounded header band
    rect_filler = patches.Rectangle(
        (x, y + h - header_h), w, header_h * 0.4, 
        facecolor=header_color, edgecolor="none", zorder=3
    )
    ax.add_patch(rect_filler)

    # Title & Subtitle in header
    ax.text(x + 0.18, y + h - 0.25, title, color="#FFFFFF", fontsize=10.2, fontweight="bold", 
            fontfamily="sans-serif", zorder=4)
    if subtitle:
        ax.text(x + 0.18, y + h - 0.48, subtitle, color="#E2E8F0", fontsize=7.6, fontfamily="sans-serif", zorder=4)

    # Body items
    start_y = y + h - header_h - 0.22
    for item in items:
        ax.text(x + 0.18, start_y, item, color="#CBD5E1", fontsize=8.0, va="top", fontfamily="sans-serif", zorder=4)
        start_y -= 0.30

def draw_step_badge(ax, x, y, text, color="#38BDF8"):
    """Draws a clean, high-contrast numbered step badge."""
    ax.text(
        x, y, text, color=color, fontsize=7.6, fontweight="bold", ha="center", va="center",
        bbox=dict(boxstyle="round,pad=0.28", facecolor="#0B0F19", edgecolor=color, lw=1.3, alpha=0.98),
        zorder=7
    )

def generate_diagram(output_path="architecture_diagram.png"):
    fig, ax = plt.subplots(figsize=(20, 11.5), dpi=300)
    ax.set_facecolor("#080D1A")
    fig.patch.set_facecolor("#080D1A")

    # Header Title Banner
    ax.text(10.0, 10.9, "GROUNDED FINANCIAL INTELLIGENCE ASSISTANT", 
            color="#F8FAFC", fontsize=21, fontweight="bold", ha="center", fontfamily="sans-serif")
    ax.text(10.0, 10.5, "End-to-End System Architecture: Intent Parsing  ->  Guardrail Gate  ->  Deterministic DuckDB OLAP  ->  Grounded Explanation", 
            color="#94A3B8", fontsize=10.5, ha="center", fontfamily="sans-serif")

    # Core Guarantees Ribbon (Top)
    guarantees = [
        (1.0, 9.65, 4.0, 0.48, "0% Arithmetic Hallucination", "#10B981"),
        (5.5, 9.65, 4.0, 0.48, "Dynamic Anchor Date (2024-05-31)", "#818CF8"),
        (10.0, 9.65, 4.0, 0.48, "SQL Parameterization Defense", "#F43F5E"),
        (14.5, 9.65, 4.0, 0.48, "100% Grounded Audit Trail & CSV", "#38BDF8"),
    ]
    for gx, gy, gw, gh, gtext, gcolor in guarantees:
        gbadge = patches.FancyBboxPatch((gx, gy), gw, gh, boxstyle="round,pad=0.05,rounding_size=0.12",
                                        facecolor="#131C2E", edgecolor=gcolor, linewidth=1.5, zorder=2)
        ax.add_patch(gbadge)
        ax.text(gx + gw/2, gy + gh/2, f"  {gtext}", color=gcolor, fontsize=8.8, fontweight="bold",
                ha="center", va="center", fontfamily="sans-serif", zorder=3)

    # -------------------------------------------------------------
    # COLUMN 1: USER & STREAMLIT INTERFACE
    # -------------------------------------------------------------
    draw_card(
        ax, x=0.8, y=2.2, w=3.6, h=6.8,
        title="STAGE 1: User & Interface",
        subtitle="Streamlit Conversational UI",
        items=[
            "• Natural Language Query Input",
            "• Multi-Turn Memory Persistence",
            "• Executive Spend & Audit Views",
            "• 1-Click CSV Breakdown Export",
            "• Full Grounded SQL Drawer",
            "• Real-Time Latency Telemetry",
            "• Visual Confidence Signalling:",
            "   - High Certainty (>95%)",
            "   - Moderate (Fuzzy / Alias)",
            "   - Low (Ambiguity Warning)"
        ],
        header_color="#0284C7"
    )

    # -------------------------------------------------------------
    # COLUMN 2: INTENT PARSER, RESOLVER & GUARDRAILS
    # -------------------------------------------------------------
    # 2A: Intent Parser
    draw_card(
        ax, x=5.2, y=6.6, w=4.0, h=2.4,
        title="STAGE 2: Intent & Temporal Parser",
        subtitle="Groq LPU (openai/gpt-oss-120b)",
        items=[
            "• Extracts JSON: Intent, Entity, Dates",
            "• Dynamic Anchor Date: Anchors 'last month'",
            "   to MAX(payout_date) = 2024-05-31",
            "• Dynamic Regex Pattern Fallback"
        ],
        header_color="#6366F1"
    )

    # 2B: Entity Resolver
    draw_card(
        ax, x=5.2, y=3.8, w=4.0, h=2.3,
        title="STAGE 2: Entity Resolver",
        subtitle="RapidFuzz + Dynamic Acronym Engine",
        items=[
            "• Dynamic Acronyms (e.g. AWS -> Amazon)",
            "• Substring & Fuzzy Match Scoring",
            "• 3-Way Routing: MATCH, AMBIGUOUS,",
            "   or NOT_FOUND in dataset"
        ],
        header_color="#8B5CF6"
    )

    # 2C: Guardrail Gate (Short-Circuit Card)
    draw_card(
        ax, x=5.2, y=1.6, w=4.0, h=1.8,
        title="SAFETY GUARDRAILS (Trap #4)",
        subtitle="Deterministic Short-Circuit Gate",
        items=[
            "• Guard 1: Vendor Not Found in Records",
            "   -> 'No data for Netflix in records'",
            "• Guard 2: Ambiguous Match (Multi-Vendor)",
            "   -> 'Did you mean AWS or Logistics?'"
        ],
        header_color="#E11D48",
        border_color="#F43F5E"
    )

    # -------------------------------------------------------------
    # COLUMN 3: DETERMINISTIC ANALYTICAL SQL & OLAP
    # -------------------------------------------------------------
    # 3A: Parameterized SQL Builder
    draw_card(
        ax, x=10.0, y=6.6, w=4.2, h=2.4,
        title="STAGE 3: Parameterized SQL Builder",
        subtitle="Deterministic Query Compiler",
        items=[
            "• Compiles strictly bound '?' parameters",
            "• SQL Injection Immune: Neutralizes payloads",
            "• Multi-state reconciliation taxonomy",
            "• Whitelist column projections"
        ],
        header_color="#059669"
    )

    # 3B: In-Memory DuckDB OLAP
    draw_card(
        ax, x=10.0, y=2.6, w=4.2, h=3.5,
        title="STAGE 3: DuckDB Analytical Engine",
        subtitle="In-Memory OLAP (Zero Math Hallucination)",
        items=[
            "• Vectorized C++ Analytical Compute",
            "• Executes SUM, AVG, COUNT in < 5ms",
            "• Zero LLM Math Computation",
            "• Joins 5 Relational Schemas:",
            "   1. vendors (whitelisted names)",
            "   2. vendor_payouts (date & amount)",
            "   3. transactions (financial txns)",
            "   4. reconciliation_status (audit states)",
            "   5. chart_of_accounts (ledger codes)"
        ],
        header_color="#10B981"
    )

    # -------------------------------------------------------------
    # COLUMN 4: STATISTICAL ANOMALIES & GROUNDED EXPLAINER
    # -------------------------------------------------------------
    # 4A: Anomaly Engine
    draw_card(
        ax, x=15.0, y=6.6, w=4.3, h=2.4,
        title="STAGE 4: Statistical Anomaly Engine",
        subtitle="Uncontaminated Baseline Outlier Detection",
        items=[
            "• Baseline excludes queried window",
            "• Flags payouts > (mean + 2*std)",
            "• Detects spikes (e.g. Acme $58.5k = 9.2x)",
            "• Contextual multiplier alert warnings"
        ],
        header_color="#D97706"
    )

    # 4B: Grounded Explainer
    draw_card(
        ax, x=15.0, y=2.6, w=4.3, h=3.5,
        title="STAGE 5: Grounded Explainer",
        subtitle="Zero-Calculation Executive Narrator",
        items=[
            "• Explains DuckDB pre-computed numbers strictly",
            "• Zero arithmetic computed in the LLM",
            "• KaTeX currency sanitation (&#36;)",
            "• Embeds statistical anomaly alerts",
            "• Compiles Complete Response Package:",
            "   [1] Plain-language narrative summary",
            "   [2] Interactive verifiable table",
            "   [3] 1-Click CSV export data",
            "   [4] Full executed SQL audit trace"
        ],
        header_color="#7C3AED"
    )

    # -------------------------------------------------------------
    # STAGE 6: BOTTOM RETURN HIGHWAY (AUDITABLE RESPONSE LOOP)
    # -------------------------------------------------------------
    highway = patches.FancyBboxPatch(
        (0.8, 0.35), 18.5, 0.85, 
        boxstyle="round,pad=0.06,rounding_size=0.15",
        facecolor="#0F172A", edgecolor="#38BDF8", linewidth=2, zorder=2
    )
    ax.add_patch(highway)
    ax.text(10.0, 0.85, "STAGE 6: COMPLETE VERIFIABLE AUDIT PACKAGE RETURNED TO STREAMLIT UI", 
            color="#38BDF8", fontsize=10.0, fontweight="bold", ha="center", fontfamily="sans-serif", zorder=3)
    ax.text(10.0, 0.55, "Every Query Returns: [1] Grounded Natural Summary  |  [2] Verifiable Table  |  [3] 1-Click CSV Export  |  [4] Full Executed SQL & Latency Telemetry",
            color="#CBD5E1", fontsize=8.2, ha="center", fontfamily="sans-serif", zorder=3)

    # -------------------------------------------------------------
    # FLOW ARROWS (Orthogonal, Strictly Non-Overlapping)
    # -------------------------------------------------------------

    # 1. User -> Intent Parser
    # From (4.4, 7.8) to (5.2, 7.8)
    ax.annotate("", xy=(5.2, 7.8), xytext=(4.4, 7.8),
                arrowprops=dict(arrowstyle="->", color="#38BDF8", lw=2.2), zorder=5)
    draw_step_badge(ax, 4.8, 8.15, "1. Natural Query", color="#38BDF8")

    # 2. Intent Parser -> Entity Resolver
    # Down from (7.2, 6.6) to (7.2, 6.1)
    ax.annotate("", xy=(7.2, 6.1), xytext=(7.2, 6.6),
                arrowprops=dict(arrowstyle="->", color="#818CF8", lw=2.2), zorder=5)
    draw_step_badge(ax, 7.2, 6.35, "2. Raw Entity", color="#818CF8")

    # 3. Guardrail Branch (Down from Resolver into Guardrails)
    # Down from (7.2, 3.8) to (7.2, 3.4)
    ax.annotate("", xy=(7.2, 3.4), xytext=(7.2, 3.8),
                arrowprops=dict(arrowstyle="->", color="#F43F5E", lw=2.2, linestyle="--"), zorder=5)
    draw_step_badge(ax, 7.2, 3.6, "Missing / Ambiguous", color="#F43F5E")

    # 3b. Guardrail Return to Stage 1 (Straight Horizontal from Guardrails left to Stage 1 right)
    # Guardrail center y is 2.5. Stage 1 right is at x=4.4, y=2.5.
    ax.annotate("", xy=(4.4, 2.5), xytext=(5.2, 2.5),
                arrowprops=dict(arrowstyle="->", color="#F43F5E", lw=2.2, linestyle="--"), zorder=5)
    draw_step_badge(ax, 4.8, 2.85, "Guardrail Return", color="#F43F5E")

    # 4. Valid Entity: Resolver -> SQL Builder
    # Route: Out of Resolver right (9.2, 4.95) -> horizontal to 9.6 -> vertical up to 7.8 -> into SQL Builder (10.0, 7.8)
    ax.plot([9.2, 9.6, 9.6, 10.0], [4.95, 4.95, 7.8, 7.8], color="#10B981", lw=2.2, zorder=5)
    ax.annotate("", xy=(10.0, 7.8), xytext=(9.9, 7.8),
                arrowprops=dict(arrowstyle="->", color="#10B981", lw=2.2), zorder=5)
    draw_step_badge(ax, 9.6, 6.35, "3. Valid Intent\n& Entity", color="#10B981")

    # 5. SQL Builder -> DuckDB
    # Down from (12.1, 6.6) to (12.1, 6.1)
    ax.annotate("", xy=(12.1, 6.1), xytext=(12.1, 6.6),
                arrowprops=dict(arrowstyle="->", color="#10B981", lw=2.2), zorder=5)
    draw_step_badge(ax, 12.1, 6.35, "4. Safe '?' SQL", color="#10B981")

    # 6. DuckDB -> Explainer (Direct Pre-Computed Table)
    # Horizontal from DuckDB right (14.2, 4.35) to Explainer left (15.0, 4.35)
    ax.annotate("", xy=(15.0, 4.35), xytext=(14.2, 4.35),
                arrowprops=dict(arrowstyle="->", color="#10B981", lw=2.2), zorder=5)
    draw_step_badge(ax, 14.6, 4.7, "5. Computed DF", color="#10B981")

    # 7. DuckDB -> Anomaly Engine (Historical Payouts)
    # Route: Out of DuckDB right (14.2, 5.5) -> horizontal to 14.6 -> vertical up to 7.8 -> into Anomaly Engine (15.0, 7.8)
    ax.plot([14.2, 14.6, 14.6, 15.0], [5.5, 5.5, 7.8, 7.8], color="#D97706", lw=2.2, zorder=5)
    ax.annotate("", xy=(15.0, 7.8), xytext=(14.9, 7.8),
                arrowprops=dict(arrowstyle="->", color="#D97706", lw=2.2), zorder=5)
    draw_step_badge(ax, 14.6, 6.65, "Historical\nPayout Data", color="#D97706")

    # 8. Anomaly Engine -> Explainer (Outlier Alerts)
    # Down from (17.15, 6.6) to (17.15, 6.1)
    ax.annotate("", xy=(17.15, 6.1), xytext=(17.15, 6.6),
                arrowprops=dict(arrowstyle="->", color="#D97706", lw=2.2), zorder=5)
    draw_step_badge(ax, 17.15, 6.35, "6. Outlier Alerts", color="#D97706")

    # 9. Explainer -> Bottom Highway
    # Down from Explainer bottom (17.15, 2.6) to Highway top (17.15, 1.2)
    ax.annotate("", xy=(17.15, 1.2), xytext=(17.15, 2.6),
                arrowprops=dict(arrowstyle="->", color="#A855F7", lw=2.2), zorder=5)
    draw_step_badge(ax, 17.15, 1.9, "7. Package Delivery", color="#A855F7")

    # 10. Bottom Highway -> Stage 1 User Interface
    # Up from Highway top left (2.6, 1.2) to Stage 1 bottom (2.6, 2.2)
    ax.annotate("", xy=(2.6, 2.2), xytext=(2.6, 1.2),
                arrowprops=dict(arrowstyle="->", color="#38BDF8", lw=2.2), zorder=5)
    draw_step_badge(ax, 2.6, 1.7, "8. Render Verified Answer", color="#38BDF8")

    ax.set_xlim(0, 20.0)
    ax.set_ylim(0.0, 11.5)
    ax.axis("off")

    plt.tight_layout()
    plt.savefig(output_path, dpi=300, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close()
    print(f"Architecture diagram generated successfully at: {output_path}")

if __name__ == "__main__":
    generate_diagram()
