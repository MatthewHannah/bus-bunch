using System.Data;
using Microsoft.Data.SqlClient;

namespace BusBunch.Functions.Services;

/// <summary>
/// Thin read-only helper around <see cref="SqlConnection"/> for the HTTP API.
/// Returns rows as <see cref="Dictionary{TKey, TValue}"/> so the function
/// layer can hand them straight to <c>System.Text.Json</c> without per-endpoint
/// row DTOs. All callers MUST use parameterized SQL.
/// </summary>
public class SqlReader
{
    private readonly string _connectionString;

    public SqlReader(SqlOptions options)
    {
        _connectionString = options.ConnectionString;
    }

    public async Task<List<Dictionary<string, object?>>> QueryAsync(
        string sql,
        IEnumerable<(string name, object? value)>? parameters,
        CancellationToken ct)
    {
        await using var conn = new SqlConnection(_connectionString);
        await conn.OpenAsync(ct);

        await using var cmd = new SqlCommand(sql, conn) { CommandTimeout = 60 };
        if (parameters is not null)
        {
            foreach (var (name, value) in parameters)
            {
                cmd.Parameters.AddWithValue(name, value ?? DBNull.Value);
            }
        }

        await using var reader = await cmd.ExecuteReaderAsync(ct);
        var columns = Enumerable.Range(0, reader.FieldCount)
            .Select(i => reader.GetName(i))
            .ToArray();

        var rows = new List<Dictionary<string, object?>>();
        while (await reader.ReadAsync(ct))
        {
            var row = new Dictionary<string, object?>(columns.Length);
            for (var i = 0; i < columns.Length; i++)
            {
                var v = reader.GetValue(i);
                row[columns[i]] = v is DBNull ? null : v;
            }
            rows.Add(row);
        }
        return rows;
    }
}
