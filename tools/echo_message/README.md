# Echo a message

Prints a message in uppercase, optionally repeated. `--uppercase` is a fixed argument from
`tool.json`; the message and the repeat count come from the model.

## Usage

```bash
python3 echo_message/echo_message.py [OPTIONS] <message>
```

## Parameters

- **message** *(string, required)*  
  The message text to echo.

- **repeat** *(integer, optional, default: 1)*  
  Number of times to repeat the message.

- **--uppercase** *(flag, static)*  
  Always applied if configured in the JSON. Converts the message to uppercase.

## Examples

```bash
# Basic usage
python3 echo_message/echo_message.py "Hello world"

# Repeat 3 times
python3 echo_message/echo_message.py "Hello" --repeat 3

# With static --uppercase (always passed by tool.json)
python3 echo_message/echo_message.py "Hello MCP" --repeat 2
```

