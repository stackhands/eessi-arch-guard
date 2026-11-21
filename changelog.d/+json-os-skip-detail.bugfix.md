For a run that ends as an OSError skip, the Zig guard prints a null `detail` in the JSON object. It used to repeat the message there, where the Python guard prints null.
