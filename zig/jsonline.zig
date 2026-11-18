//! Shared one-line JSON formatting helpers for Python-style spacing.
const std = @import("std");

const Allocator = std.mem.Allocator;

pub fn compactJsonLine(arena: Allocator, pretty: []const u8) Allocator.Error![]u8 {
    var out: std.ArrayList(u8) = .empty;
    var in_string = false;
    var escaped = false;
    for (pretty) |ch| {
        if (in_string) {
            try out.append(arena, ch);
            if (escaped) {
                escaped = false;
            } else if (ch == '\\') {
                escaped = true;
            } else if (ch == '"') {
                in_string = false;
            }
            continue;
        }
        if (ch == '"') {
            in_string = true;
            try out.append(arena, ch);
            continue;
        }
        if (ch == '\n' or ch == '\r' or ch == ' ' or ch == '\t') continue;
        try out.append(arena, ch);
    }
    return out.items;
}

/// Match Python's ``json.dumps`` separators outside quoted strings.
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
        if (ch == ':') {
            try out.appendSlice(arena, ": ");
            continue;
        }
        try out.append(arena, ch);
    }
    return out.items;
}
