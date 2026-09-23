use strict;
use warnings;
while (<STDIN>) {
    print if /\bERROR\b/;
}
