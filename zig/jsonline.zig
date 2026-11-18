//! Shared one-line JSON formatting helpers for Python-style spacing.
const std = @import("std");

const Allocator = std.mem.Allocator;

pub fn compactJsonLine(arena: Allocator, pretty: []const u8) Allocator.Error![]u8 {
    var out: std.ArrayList(u8) = .empty;
    for (pretty) |ch| {
        if (ch != '\n') try out.append(arena, ch) else if (out.items.len > 0 and out.items[out.items.len - 1] == ' ') {
            _ = out.pop();
        }
    }
    return out.items;
}

/// Match Python's ``json.dumps`` comma spacing outside quoted strings.
pub fn pythonJsonCommas(arena: Allocator, s: []const u8) Allocator.Error![]u8 {
    var out: std.ArrayList(u8) = .empty;
    var in_str = false;
    var esc = false;
    for (s) |ch| {
        if (in_str) {
            try out.append(arena, ch);
            if (esc) esc = false else if (ch == '\\') esc = true else if (ch == '"') in_str = false;
            continue;
        }
        if (ch == '"') {
            in_str = true;
            try out.append(arena, ch);
            continue;
        }
        if (ch == ',') {
            try out.appendSlice(arena, ", ");
            continue;
        }
        try out.append(arena, ch);
    }
    return out.items;
}
