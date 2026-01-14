import io
import logging
import math
from datetime import datetime
from typing import List

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

# Color palette - dark theme
COLORS = {
    "bg": (18, 18, 24),
    "bg_card": (28, 28, 38),
    "grid": (45, 45, 60),
    "text": (220, 220, 230),
    "text_dim": (140, 140, 160),
    "accent": (99, 179, 237),  # Blue for latency line
    "accent_fill": (99, 179, 237, 40),
    "success": (72, 187, 120),  # Green for up
    "error": (245, 101, 101),  # Red for down
    "warning": (236, 201, 75),  # Yellow for flapping/slow
}


def _get_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Try to load a decent font, fall back to default."""
    font_paths = [
        "/usr/share/fonts/TTF/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "C:\\Windows\\Fonts\\segoeui.ttf",
    ]
    for path in font_paths:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


def _draw_rounded_rect(draw: ImageDraw.ImageDraw, xy: tuple, radius: int, fill: tuple):
    """Draw a rounded rectangle."""
    x1, y1, x2, y2 = xy
    draw.rectangle([x1 + radius, y1, x2 - radius, y2], fill=fill)
    draw.rectangle([x1, y1 + radius, x2, y2 - radius], fill=fill)
    draw.pieslice([x1, y1, x1 + radius * 2, y1 + radius * 2], 180, 270, fill=fill)
    draw.pieslice([x2 - radius * 2, y1, x2, y1 + radius * 2], 270, 360, fill=fill)
    draw.pieslice([x1, y2 - radius * 2, x1 + radius * 2, y2], 90, 180, fill=fill)
    draw.pieslice([x2 - radius * 2, y2 - radius * 2, x2, y2], 0, 90, fill=fill)


def _draw_line_thick(draw: ImageDraw.ImageDraw, points: list, color: tuple, width: int = 2):
    """Draw a thicker anti-aliased line by drawing multiple lines."""
    for i in range(len(points) - 1):
        x1, y1 = points[i]
        x2, y2 = points[i + 1]
        draw.line([(x1, y1), (x2, y2)], fill=color, width=width)


async def generate_monitor_charts(
    monitor_name: str, check_runs: list, time_range: str = "24h"
) -> io.BytesIO:
    """
    Generates a combined chart (latency line chart + uptime bar) for a monitor.

    Args:
        monitor_name: The name of the monitor.
        check_runs: A list of CheckRun objects (recent history, newest first).
        time_range: Time range label for display (e.g., "6h", "24h", "1month").

    Returns:
        A BytesIO object containing the PNG image data.
    """
    # Reverse to have oldest first for plotting
    runs = list(reversed(check_runs))

    if not runs:
        return _generate_empty_chart(monitor_name)

    # Dimensions
    width, height = 900, 500
    padding = {"top": 80, "right": 40, "bottom": 120, "left": 80}
    chart_width = width - padding["left"] - padding["right"]
    chart_height = height - padding["top"] - padding["bottom"]

    # Create image
    img = Image.new("RGB", (width, height), COLORS["bg"])
    draw = ImageDraw.Draw(img)

    # Fonts
    font_title = _get_font(20)
    font_label = _get_font(14)
    font_small = _get_font(11)
    font_stat = _get_font(16)

    # Extract data
    latencies = [r.latency_ms for r in runs]
    statuses = [r.is_up for r in runs]
    timestamps = [r.timestamp for r in runs]

    # Calculate statistics
    avg_latency = sum(latencies) / len(latencies)
    min_latency = min(latencies)
    max_latency = max(latencies)
    uptime_pct = (sum(statuses) / len(statuses)) * 100 if statuses else 0

    # Draw title area
    title = monitor_name
    draw.text((padding["left"], 25), title, fill=COLORS["text"], font=font_title)

    # Draw time range label
    range_labels = {
        "1h": "Last hour",
        "6h": "Last 6 hours",
        "24h": "Last 24 hours",
        "1month": "Last month",
    }
    range_text = range_labels.get(time_range, time_range)
    draw.text((padding["left"], 50), range_text, fill=COLORS["text_dim"], font=font_small)

    # Draw stats cards at top right
    stats_x = width - padding["right"] - 320
    _draw_rounded_rect(draw, (stats_x, 15, stats_x + 75, 55), 6, COLORS["bg_card"])
    _draw_rounded_rect(draw, (stats_x + 85, 15, stats_x + 160, 55), 6, COLORS["bg_card"])
    _draw_rounded_rect(draw, (stats_x + 170, 15, stats_x + 245, 55), 6, COLORS["bg_card"])
    _draw_rounded_rect(draw, (stats_x + 255, 15, stats_x + 320, 55), 6, COLORS["bg_card"])

    draw.text((stats_x + 8, 18), "AVG", fill=COLORS["text_dim"], font=font_small)
    draw.text((stats_x + 8, 33), f"{avg_latency:.0f}ms", fill=COLORS["accent"], font=font_stat)

    draw.text((stats_x + 93, 18), "MIN", fill=COLORS["text_dim"], font=font_small)
    draw.text((stats_x + 93, 33), f"{min_latency:.0f}ms", fill=COLORS["success"], font=font_stat)

    draw.text((stats_x + 178, 18), "MAX", fill=COLORS["text_dim"], font=font_small)
    draw.text((stats_x + 178, 33), f"{max_latency:.0f}ms", fill=COLORS["error"], font=font_stat)

    draw.text((stats_x + 263, 18), "UP", fill=COLORS["text_dim"], font=font_small)
    uptime_color = (
        COLORS["success"]
        if uptime_pct >= 99
        else COLORS["warning"] if uptime_pct >= 95 else COLORS["error"]
    )
    draw.text((stats_x + 263, 33), f"{uptime_pct:.1f}%", fill=uptime_color, font=font_stat)

    # Draw chart background
    chart_x = padding["left"]
    chart_y = padding["top"]
    _draw_rounded_rect(
        draw,
        (chart_x - 10, chart_y - 10, chart_x + chart_width + 10, chart_y + chart_height + 10),
        8,
        COLORS["bg_card"],
    )

    # Calculate Y axis range with some padding (logarithmic)
    def to_log(v):
        return math.log(max(v, 0.1), 10)

    def from_log(lv):
        return math.pow(10, lv)

    y_min_val = 0
    y_max_val = max_latency * 1.5 if max_latency > 0 else 100

    # We want the log scale to start from a reasonable minimum, e.g., 1ms or 10ms
    # but still show 0 if that's what we have.
    # Using a floor for the log calculation.
    log_min = to_log(1)  # Start scale from 1ms for better visibility
    log_max = to_log(y_max_val)
    log_range = log_max - log_min if log_max > log_min else 1

    # Draw grid lines and Y axis labels (logarithmic)
    num_grid_lines = 5
    # For log scale, we might want specific values like 1, 10, 100, 1000
    # but for simplicity, we'll divide the log range equally.
    for i in range(num_grid_lines + 1):
        log_val = log_min + (log_range * i / num_grid_lines)
        y_pos = chart_y + chart_height - (i / num_grid_lines) * chart_height

        # Grid line
        draw.line([(chart_x, y_pos), (chart_x + chart_width, y_pos)], fill=COLORS["grid"], width=1)

        # Y axis label
        actual_val = from_log(log_val)
        label = f"{actual_val:.0f}" if actual_val >= 1 else f"{actual_val:.1f}"
        draw.text((chart_x - 45, y_pos - 7), label, fill=COLORS["text_dim"], font=font_small)

    # Draw Y axis title
    draw.text(
        (15, chart_y + chart_height // 2 - 20), "Latency", fill=COLORS["text_dim"], font=font_small
    )
    draw.text(
        (15, chart_y + chart_height // 2), "(ms, log)", fill=COLORS["text_dim"], font=font_small
    )

    # Calculate time range for x-axis (time-based placement)
    time_min = min(r.timestamp for r in runs)
    time_max = max(r.timestamp for r in runs)
    time_span = (time_max - time_min).total_seconds()
    if time_span == 0:
        time_span = 1  # Avoid division by zero for single point

    # Calculate points for the line chart (time-based x-axis, log-based y-axis)
    points = []
    for run in runs:
        # Time-based x position
        time_offset = (run.timestamp - time_min).total_seconds()
        x = chart_x + (time_offset / time_span) * chart_width

        # Log-based y position
        run_log_val = to_log(run.latency_ms)
        y = chart_y + chart_height - ((run_log_val - log_min) / log_range) * chart_height
        y = max(chart_y, min(chart_y + chart_height, y))  # Clamp
        points.append((x, y))

    # Draw filled area + line segments (color-coded: red when down)
    if len(points) >= 2:
        overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        overlay_draw = ImageDraw.Draw(overlay)
        baseline_y = chart_y + chart_height

        for i in range(len(points) - 1):
            (x1, y1) = points[i]
            (x2, y2) = points[i + 1]

            # If either endpoint is a failed check, color this segment as "down"
            seg_is_down = (not runs[i].is_up) or (not runs[i + 1].is_up)

            line_color = COLORS["error"] if seg_is_down else COLORS["accent"]
            fill_color = (245, 101, 101, 35) if seg_is_down else (99, 179, 237, 30)

            # Fill polygon for this segment
            overlay_draw.polygon(
                [(x1, y1), (x2, y2), (x2, baseline_y), (x1, baseline_y)], fill=fill_color
            )

            # Draw the segment line
            _draw_line_thick(draw, [(x1, y1), (x2, y2)], line_color, width=2)

        img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
        draw = ImageDraw.Draw(img)

    # Draw data points
    for i, (x, y) in enumerate(points):
        radius = 4 if i == len(points) - 1 else 3  # Larger dot for latest
        color = COLORS["accent"]
        if not runs[i].is_up:
            color = COLORS["error"]
        draw.ellipse([x - radius, y - radius, x + radius, y + radius], fill=color)

    # Draw uptime status bar at bottom (time-based)
    bar_y = chart_y + chart_height + 30
    bar_height = 20
    draw.text((chart_x, bar_y - 18), "Status History", fill=COLORS["text_dim"], font=font_small)

    # Sort runs by timestamp for the status bar
    sorted_runs = sorted(runs, key=lambda r: r.timestamp)

    # Calculate segment width based on time intervals
    for i, run in enumerate(sorted_runs):
        time_offset = (run.timestamp - time_min).total_seconds()
        seg_x = chart_x + (time_offset / time_span) * chart_width

        # Determine segment width (to next point or end)
        if i < len(sorted_runs) - 1:
            next_offset = (sorted_runs[i + 1].timestamp - time_min).total_seconds()
            next_x = chart_x + (next_offset / time_span) * chart_width
            seg_width = next_x - seg_x
        else:
            seg_width = chart_x + chart_width - seg_x

        color = COLORS["success"] if run.is_up else COLORS["error"]

        # Draw segment with small gap
        gap = 1 if seg_width > 4 else 0
        draw.rectangle(
            [seg_x + gap, bar_y, seg_x + seg_width - gap, bar_y + bar_height], fill=color
        )

    # Draw time labels on X axis (time-based)
    if timestamps:
        # Show first, middle, and last timestamps based on actual time
        time_positions = [time_min, time_min + (time_max - time_min) / 2, time_max]
        for ts in time_positions:
            if isinstance(ts, datetime):
                time_offset = (ts - time_min).total_seconds()
                x = chart_x + (time_offset / time_span) * chart_width
                time_str = ts.strftime("%H:%M")
                date_str = ts.strftime("%m/%d")

                # Center the text
                draw.text(
                    (x - 15, bar_y + bar_height + 8),
                    time_str,
                    fill=COLORS["text_dim"],
                    font=font_small,
                )
                draw.text(
                    (x - 15, bar_y + bar_height + 22),
                    date_str,
                    fill=COLORS["text_dim"],
                    font=font_small,
                )

    # Draw legend
    legend_y = height - 25
    draw.ellipse([chart_x, legend_y - 4, chart_x + 8, legend_y + 4], fill=COLORS["success"])
    draw.text((chart_x + 12, legend_y - 6), "Up", fill=COLORS["text_dim"], font=font_small)

    draw.ellipse([chart_x + 50, legend_y - 4, chart_x + 58, legend_y + 4], fill=COLORS["error"])
    draw.text((chart_x + 62, legend_y - 6), "Down", fill=COLORS["text_dim"], font=font_small)

    draw.line(
        [(chart_x + 120, legend_y), (chart_x + 140, legend_y)], fill=COLORS["accent"], width=2
    )
    draw.text((chart_x + 145, legend_y - 6), "Latency", fill=COLORS["text_dim"], font=font_small)

    # Save to buffer
    buf = io.BytesIO()
    img.save(buf, format="PNG", quality=95)
    buf.seek(0)
    return buf


async def generate_dashboard_chart(monitors_data: list) -> io.BytesIO:
    """
    Generates a single dashboard image containing mini-charts for all monitors.
    monitors_data: list of dicts {name, recent_runs, time_range}
    """
    if not monitors_data:
        return _generate_empty_dashboard()

    # Layout: 2 columns
    num_monitors = len(monitors_data)
    cols = 2
    rows = math.ceil(num_monitors / cols)

    # Mini chart dimensions
    mini_width = 450
    mini_height = 250

    total_width = mini_width * cols
    total_height = mini_height * rows + 60  # extra space for header

    img = Image.new("RGB", (total_width, total_height), COLORS["bg"])
    draw = ImageDraw.Draw(img)

    font_title = _get_font(24)
    font_small = _get_font(12)

    # Draw header
    draw.text((20, 15), "Service Dashboard", fill=COLORS["text"], font=font_title)
    now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    draw.text((total_width - 200, 25), now_str, fill=COLORS["text_dim"], font=font_small)

    for i, data in enumerate(monitors_data):
        row = i // cols
        col = i % cols

        x_offset = col * mini_width
        y_offset = row * mini_height + 60

        # Generate mini chart for this monitor
        # We can reuse generate_monitor_charts logic but scaled down
        # For now, let's just draw a simplified version directly here
        _draw_mini_monitor_chart(draw, x_offset, y_offset, mini_width, mini_height, data)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


def _draw_mini_monitor_chart(draw, x, y, width, height, data):
    """Draws a simplified monitor chart in a sub-region."""
    padding = 15
    inner_x = x + padding
    inner_y = y + padding
    inner_w = width - 2 * padding
    inner_h = height - 2 * padding

    # Background card
    _draw_rounded_rect(draw, (x + 5, y + 5, x + width - 5, y + height - 5), 8, COLORS["bg_card"])

    name = data["name"]
    runs = list(reversed(data["recent_runs"]))
    if not runs:
        draw.text(
            (inner_x + 10, inner_y + 10),
            f"{name}: No data",
            fill=COLORS["text_dim"],
            font=_get_font(14),
        )
        return

    # Title & Status
    latest = runs[-1]
    status_color = COLORS["success"] if latest.is_up else COLORS["error"]
    draw.ellipse([inner_x, inner_y + 5, inner_x + 10, inner_y + 15], fill=status_color)
    draw.text((inner_x + 18, inner_y), name[:25], fill=COLORS["text"], font=_get_font(16))

    # Latency & Uptime
    latencies = [r.latency_ms for r in runs]
    avg_lat = sum(latencies) / len(latencies)
    uptime_pct = (sum(1 for r in runs if r.is_up) / len(runs)) * 100

    stats_text = f"Avg: {avg_lat:.0f}ms | Up: {uptime_pct:.1f}%"
    draw.text((inner_x, inner_y + 25), stats_text, fill=COLORS["text_dim"], font=_get_font(11))

    # Mini Sparkline
    chart_y = inner_y + 50
    chart_h = inner_h - 60

    if len(runs) >= 2:
        max_lat = max(latencies) if max(latencies) > 0 else 100

        # Log scale for sparkline too
        def to_log(v):
            return math.log(max(v, 0.1), 10)

        log_min = to_log(1)
        log_max = to_log(max_lat * 1.2)
        log_range = log_max - log_min if log_max > log_min else 1

        points = []
        for i, r in enumerate(runs):
            px = inner_x + (i / (len(runs) - 1)) * inner_w
            py = chart_y + chart_h - ((to_log(r.latency_ms) - log_min) / log_range) * chart_h
            points.append((px, py))

        # Draw line
        for i in range(len(points) - 1):
            color = COLORS["accent"] if runs[i + 1].is_up else COLORS["error"]
            draw.line([points[i], points[i + 1]], fill=color, width=2)

    # Status Bar at bottom of mini card
    bar_y = y + height - 20
    bar_h = 8
    num_runs = len(runs)
    seg_w = inner_w / num_runs
    for i, r in enumerate(runs):
        color = COLORS["success"] if r.is_up else COLORS["error"]
        x0 = inner_x + i * seg_w
        x1 = inner_x + (i + 1) * seg_w
        # Ensure at least 1 pixel gap between segments, but x1 must be >= x0
        if i < num_runs - 1:
            x1 = max(x0 + 1, x1 - 1)  # Leave 1px gap, but ensure valid rectangle
        else:
            x1 = max(x0 + 1, x1)  # Last segment goes to edge, but ensure valid rectangle
        draw.rectangle([x0, bar_y, x1, bar_y + bar_h], fill=color)


def _generate_empty_dashboard() -> io.BytesIO:
    width, height = 900, 200
    img = Image.new("RGB", (width, height), COLORS["bg"])
    draw = ImageDraw.Draw(img)
    draw.text(
        (width // 2 - 100, height // 2),
        "No Monitors Configured",
        fill=COLORS["text_dim"],
        font=_get_font(18),
    )
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


def _generate_empty_chart(monitor_name: str) -> io.BytesIO:
    """Generate a placeholder chart when no data is available."""
    width, height = 900, 500
    img = Image.new("RGB", (width, height), COLORS["bg"])
    draw = ImageDraw.Draw(img)

    font = _get_font(18)
    font_small = _get_font(14)

    # Center text
    text = "No Data Available"
    draw.text((width // 2 - 80, height // 2 - 20), text, fill=COLORS["text_dim"], font=font)
    draw.text(
        (width // 2 - 120, height // 2 + 10),
        f"{monitor_name} has no check history yet",
        fill=COLORS["text_dim"],
        font=font_small,
    )

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf
