import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.List;

/** Course project helper: prints line, word and character counts like a tiny wc. */
public class FileStats {
    public static void main(String[] args) throws IOException {
        if (args.length == 0) {
            System.err.println("usage: java FileStats <file>...");
            System.exit(2);
        }
        for (String name : args) {
            Path path = Paths.get(name);
            List<String> lines = Files.readAllLines(path);
            long words = lines.stream().mapToLong(l -> l.isBlank() ? 0 : l.trim().split("\\s+").length).sum();
            long chars = Files.size(path);
            System.out.printf("%8d %8d %8d %s%n", lines.size(), words, chars, name);
        }
    }
}
