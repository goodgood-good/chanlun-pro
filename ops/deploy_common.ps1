function ConvertTo-ProcessStartDate {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][object]$Value)

    if ($Value -is [datetime]) {
        return [datetime]$Value
    }
    if ($Value -is [datetimeoffset]) {
        return ([datetimeoffset]$Value).LocalDateTime
    }

    $text = [string]$Value
    try {
        return [Management.ManagementDateTimeConverter]::ToDateTime($text)
    } catch {
        $parsed = [datetime]::MinValue
        if ([datetime]::TryParse($text, [ref]$parsed)) {
            return $parsed
        }
        throw "Unsupported process creation date: $text"
    }
}
