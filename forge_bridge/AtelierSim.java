/*
 * forge_bridge/AtelierSim.java — Atelier's own headless Forge match launcher.
 *
 * Forge's stock `sim` mode (forge.view.SimulateMatch) hardcodes its AI players:
 * GamePlayerUtil.createAiPlayer(name, avatar) never passes AIOption.USE_SIMULATION,
 * so the lookahead "simulation AI" — the one that stops firing every activated
 * ability greedily — is unreachable from the command line, and the personality
 * profile can only come from the preferences file. This launcher replicates the
 * exact Commander flow of SimulateMatch.simulate() (same bootstrap, same player
 * naming, same game-log output via SimulateMatch.simulateSingleMatch) but exposes
 * both knobs as flags. Compiled against the release jar with the portable JDK —
 * no Maven, no fork build needed (that comes later, for the LLM coach seam).
 *
 * Usage:
 *   java -cp <forge-jar>:<classes-dir> AtelierSim \
 *     -d /abs/path/deck1.dck /abs/path/deck2.dck [...] \
 *     [-c 300] [-p Default|Cautious|Reckless|Experimental] [-sim]
 *
 *   -d    absolute paths to .dck files (2+, one seat each, in seat order)
 *   -c    Forge game clock in seconds before it calls a draw (default 300)
 *   -p    AI personality profile for every seat (default: whatever the
 *         preferences file says, which falls back to "Default")
 *   -sim  enable AIOption.USE_SIMULATION (lookahead AI) for every seat
 *
 * Output is the same typed game log atelier/forge_engine.py already parses
 * ("Turn:", "Add To Stack:", ..., "Game Result:"), because the log printing
 * lives in SimulateMatch.simulateSingleMatch, which is reused verbatim.
 */

import forge.GuiDesktop;
import forge.LobbyPlayer;
import forge.ai.AIOption;
import forge.deck.Deck;
import forge.deck.io.DeckSerializer;
import forge.error.ExceptionHandler;
import forge.game.GameRules;
import forge.game.GameType;
import forge.game.Match;
import forge.game.player.RegisteredPlayer;
import forge.gui.GuiBase;
import forge.model.FModel;
import forge.player.GamePlayerUtil;
import forge.view.SimulateMatch;

import java.io.File;
import java.util.ArrayList;
import java.util.Collections;
import java.util.EnumSet;
import java.util.List;
import java.util.Set;

public final class AtelierSim {

    public static void main(final String[] args) {
        final List<String> deckPaths = new ArrayList<>();
        int clockSeconds = 300;
        String profile = "";   // empty -> createAiPlayer falls back to the preferences file
        boolean useSimulation = false;

        for (int i = 0; i < args.length; i++) {
            switch (args[i]) {
                case "-d":
                    while (i + 1 < args.length && !args[i + 1].startsWith("-")) {
                        deckPaths.add(args[++i]);
                    }
                    break;
                case "-c":
                    clockSeconds = Integer.parseInt(args[++i]);
                    break;
                case "-p":
                    profile = args[++i];
                    break;
                case "-sim":
                    useSimulation = true;
                    break;
                default:
                    System.err.println("Unknown argument: " + args[i]);
                    System.exit(2);
            }
        }
        if (deckPaths.size() < 2) {
            System.err.println("Need at least two -d deck files.");
            System.exit(2);
        }

        // Same bootstrap forge.view.Main performs before entering sim mode.
        GuiBase.setInterface(new GuiDesktop());
        ExceptionHandler.registerErrorHandling();
        FModel.initialize(null, null);

        System.out.println("Simulation mode (AtelierSim: profile="
                + (profile.isEmpty() ? "<preferences>" : profile)
                + ", useSimulation=" + useSimulation + ")");

        final GameRules rules = new GameRules(GameType.Commander);
        rules.setAppliedVariants(EnumSet.of(GameType.Commander));
        rules.setGamesPerMatch(1);
        rules.setSimTimeout(clockSeconds);

        final Set<AIOption> options = useSimulation
                ? EnumSet.of(AIOption.USE_SIMULATION)
                : Collections.<AIOption>emptySet();

        final List<RegisteredPlayer> players = new ArrayList<>();
        int seat = 1;
        for (final String path : deckPaths) {
            final Deck deck = DeckSerializer.fromFile(new File(path));
            if (deck == null) {
                System.err.println("Could not load deck - " + path + ", match cannot start");
                System.exit(2);
            }
            // Seat naming must stay "Ai(<seat>)-<deck name>" — forge_engine.py's
            // _PLAYER_RE and the whole log parser key off it.
            final String name = "Ai(" + seat + ")-" + deck.getName();
            final LobbyPlayer ai = GamePlayerUtil.createAiPlayer(name, 0, 0, options, profile);
            players.add(RegisteredPlayer.forCommander(deck).setPlayer(ai));
            seat++;
        }

        final Match match = new Match(rules, players, "AtelierSim");
        SimulateMatch.simulateSingleMatch(match, 0, true);
        System.out.flush();
        System.exit(0);
    }

    private AtelierSim() {
    }
}
