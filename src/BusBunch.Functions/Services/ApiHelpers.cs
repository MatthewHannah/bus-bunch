using System.Globalization;
using System.Text.Json;
using System.Text.Json.Serialization;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Mvc;

namespace BusBunch.Functions.Services;

/// <summary>
/// Shared JSON / response helpers for the HTTP API. Centralizes the
/// (1) JSON options used by every endpoint (UTC ISO 8601 datetimes, no
/// per-row Dictionary-of-Dictionary defaults) and (2) a tiny query-string
/// parser that distinguishes "not set" from "empty string".
/// </summary>
internal static class ApiHelpers
{
    public static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNamingPolicy = SnakeToCamelCasePolicy.Instance,
        // The SQL reader returns each row as a Dictionary<string, object?>
        // keyed by the SQL column name (snake_case). System.Text.Json's
        // built-in JsonNamingPolicy.CamelCase only lower-cases the first
        // letter, so we also need DictionaryKeyPolicy with our snake→camel
        // converter to get e.g. "route_short_name" → "routeShortName".
        DictionaryKeyPolicy = SnakeToCamelCasePolicy.Instance,
        DefaultIgnoreCondition = JsonIgnoreCondition.Never,
        Converters =
        {
            new UtcDateTimeConverter(),
        },
    };

    public static IActionResult JsonOk(object payload) =>
        new JsonResult(payload, JsonOptions);

    public static IActionResult BadRequest(string message) =>
        new BadRequestObjectResult(new { error = message });

    public static string? GetQuery(HttpRequest req, string key)
    {
        if (!req.Query.TryGetValue(key, out var v)) return null;
        var s = v.ToString();
        return string.IsNullOrWhiteSpace(s) ? null : s;
    }

    public static int GetQueryInt(HttpRequest req, string key, int defaultValue)
    {
        var s = GetQuery(req, key);
        if (s is null) return defaultValue;
        return int.TryParse(s, NumberStyles.Integer, CultureInfo.InvariantCulture, out var n)
            ? n
            : defaultValue;
    }

    public static int? GetQueryNullableInt(HttpRequest req, string key)
    {
        var s = GetQuery(req, key);
        if (s is null) return null;
        return int.TryParse(s, NumberStyles.Integer, CultureInfo.InvariantCulture, out var n)
            ? n
            : null;
    }

    public static DateTime? GetQueryUtc(HttpRequest req, string key)
    {
        var s = GetQuery(req, key);
        if (s is null) return null;
        if (!DateTime.TryParse(
                s, CultureInfo.InvariantCulture,
                DateTimeStyles.AdjustToUniversal | DateTimeStyles.AssumeUniversal,
                out var dt))
        {
            return null;
        }
        return DateTime.SpecifyKind(dt, DateTimeKind.Utc);
    }

    /// <summary>
    /// snake_case → camelCase JsonNamingPolicy. Applied to both property
    /// names and dictionary keys (the row dictionaries from SqlReader are
    /// keyed by snake_case SQL column names).
    /// </summary>
    private sealed class SnakeToCamelCasePolicy : JsonNamingPolicy
    {
        public static readonly SnakeToCamelCasePolicy Instance = new();

        public override string ConvertName(string name)
        {
            if (string.IsNullOrEmpty(name)) return name;
            if (name.IndexOf('_') < 0)
            {
                // No underscores → just lower-case the first letter so a
                // PascalCase property still becomes camelCase.
                return char.IsUpper(name[0])
                    ? char.ToLowerInvariant(name[0]) + name[1..]
                    : name;
            }

            var sb = new System.Text.StringBuilder(name.Length);
            var upperNext = false;
            var first = true;
            foreach (var c in name)
            {
                if (c == '_')
                {
                    upperNext = !first;   // leading underscores are preserved as nothing
                    continue;
                }
                if (upperNext)
                {
                    sb.Append(char.ToUpperInvariant(c));
                    upperNext = false;
                }
                else
                {
                    sb.Append(first ? char.ToLowerInvariant(c) : c);
                }
                first = false;
            }
            return sb.ToString();
        }
    }

    /// <summary>
    /// <see cref="Microsoft.Data.SqlClient.SqlDataReader"/> returns DateTimes
    /// with <see cref="DateTimeKind.Unspecified"/>; our convention is that
    /// every DateTime in the database is UTC. Without this converter the
    /// serializer would emit "2024-01-01T00:00:00" with no zone indicator,
    /// which the browser (Plotly + dayjs) interprets as local time. We
    /// normalize to ISO 8601 with a trailing 'Z' so the frontend doesn't
    /// have to know about this quirk.
    /// </summary>
    private sealed class UtcDateTimeConverter : JsonConverter<DateTime>
    {
        public override DateTime Read(
            ref Utf8JsonReader reader, Type typeToConvert, JsonSerializerOptions options)
        {
            var s = reader.GetString();
            return s is null
                ? default
                : DateTime.Parse(
                    s, CultureInfo.InvariantCulture,
                    DateTimeStyles.AdjustToUniversal | DateTimeStyles.AssumeUniversal);
        }

        public override void Write(
            Utf8JsonWriter writer, DateTime value, JsonSerializerOptions options)
        {
            var utc = value.Kind switch
            {
                DateTimeKind.Utc         => value,
                DateTimeKind.Local       => value.ToUniversalTime(),
                _                        => DateTime.SpecifyKind(value, DateTimeKind.Utc),
            };
            writer.WriteStringValue(utc.ToString("yyyy-MM-ddTHH:mm:ssZ", CultureInfo.InvariantCulture));
        }
    }
}
