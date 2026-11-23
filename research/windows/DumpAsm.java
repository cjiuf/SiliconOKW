// Ghidra headless post-script: bounded linear disassembly at given addresses (no flow-follow, no decompiler).
// Usage: -postScript DumpAsm.java 0x141C2DBF0 [size]
import ghidra.app.script.GhidraScript;
import ghidra.app.cmd.disassemble.DisassembleCommand;
import ghidra.program.model.address.Address;
import ghidra.program.model.address.AddressSet;
import ghidra.program.model.listing.Instruction;
import ghidra.program.model.listing.InstructionIterator;

public class DumpAsm extends GhidraScript {
    @Override
    public void run() throws Exception {
        String[] args = getScriptArgs();
        long size = 0x400;
        int narg = args.length;
        if (narg > 1) {
            try { size = Long.decode(args[narg - 1]); narg = narg - 1; } catch (Exception e) { narg = args.length; }
        }
        for (int k = 0; k < narg; k++) {
            Address base = toAddr(args[k]);
            println("");
            println("############ " + args[k] + " ############");
            AddressSet set = new AddressSet(base, base.add(size - 1));
            try {
                DisassembleCommand dc = new DisassembleCommand(set, set, false);
                dc.applyTo(currentProgram, monitor);
            } catch (Exception e) {
                println("!! disassemble failed: " + e);
            }
            InstructionIterator ii = currentProgram.getListing().getInstructions(set, true);
            int n = 0;
            while (ii.hasNext() && n < 600) {
                Instruction i = ii.next();
                String flow = "";
                try {
                    if (i.getFlowType().isTerminal()) { flow = "   ; TERMINAL"; }
                } catch (Exception e) { }
                println("  " + i.getAddress() + "  " + i.toString() + flow);
                n++;
            }
            println("---- end " + args[k] + " (" + n + " insns) ----");
        }
    }
}
