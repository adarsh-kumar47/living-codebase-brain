import org.objectweb.asm.*;
import java.io.*;
import java.nio.file.*;
import java.util.*;
import java.util.jar.JarEntry;
import java.util.jar.JarFile;

/**
 * Bytecode -> CSV extractor for the Living Code Base Brain graph.
 *
 * Reads .class files from a compiled classes directory (first-party code) and from
 * dependency JARs, and emits CSV rows describing classes, methods, inheritance and
 * call sites.
 *
 * Bytecode is used rather than decompiled source because a .class file states call
 * targets exactly: owner, name and JVM descriptor are all present, so overloads are
 * disambiguated without any type inference.
 *
 * Also recovers Spring dependency-injection wiring (phase 10) from annotations, which
 * are themselves ordinary bytecode attributes ASM already knows how to visit: stereotype
 * annotations on classes (@Component/@Service/@Repository/@Controller/@RestController/
 * @Configuration), @Bean factory methods, and injection points (constructor parameters --
 * including the common case with *no* @Autowired at all, since modern Spring implicitly
 * wires a class's sole constructor -- plus @Autowired fields/setters). This does not
 * resolve call *sites*: where CHA already finds the concrete target unambiguous (the
 * common case for an interface with exactly one implementation), DI information adds
 * nothing beyond it. The gap it closes is the one CHA cannot: which concrete bean a given
 * injection point actually receives, including repository interfaces with no static
 * implementation at all (Spring Data proxies them at runtime) -- reported as unresolved,
 * not invented.
 */
public class Extractor {

    // Known by literal annotation descriptor, not by resolving Spring's own
    // meta-annotations (e.g. that @Service is itself meta-annotated @Component) --
    // every major static-analysis tool for Spring does the same; the alternative
    // requires parsing Spring's own classes' annotations recursively for a gain
    // this project has no use for (stereotype identity, not meta-annotation chains).
    static final Map<String, String> STEREOTYPES = Map.of(
            "Lorg/springframework/stereotype/Component;", "Component",
            "Lorg/springframework/stereotype/Service;", "Service",
            "Lorg/springframework/stereotype/Repository;", "Repository",
            "Lorg/springframework/stereotype/Controller;", "Controller",
            "Lorg/springframework/web/bind/annotation/RestController;", "RestController",
            "Lorg/springframework/context/annotation/Configuration;", "Configuration"
    );
    static final String AUTOWIRED_DESC = "Lorg/springframework/beans/factory/annotation/Autowired;";
    static final String QUALIFIER_DESC = "Lorg/springframework/beans/factory/annotation/Qualifier;";
    static final String BEAN_DESC = "Lorg/springframework/context/annotation/Bean;";

    // ---- output ----------------------------------------------------------

    static PrintWriter classesOut, methodsOut, callsOut, extendsOut, implementsOut;
    static PrintWriter beansOut, beanMethodsOut, injectionsOut;

    static int classCount = 0, methodCount = 0, callCount = 0;
    static int exactCalls = 0, virtualCalls = 0, indyCalls = 0, indyResolved = 0;
    static int beanCount = 0, beanMethodCount = 0, injectionCount = 0;

    /** Internal names of every class we actually read, for resolution stats. */
    static final Set<String> knownClasses = new HashSet<>();
    /** Call targets we emitted, to measure how many land inside the indexed world. */
    static final List<String> callTargets = new ArrayList<>();

    /** Application classes already read, so a copy of one inside a JAR -- a sibling
     *  module's own artifact on the dependency classpath -- is not indexed again as a
     *  dependency. */
    static final Set<String> appClasses = new HashSet<>();
    static int shadowedJarClasses = 0;

    public static void main(String[] args) throws Exception {
        // --classes and --jars repeat: a multi-module build has one classes directory per
        // module, and dependencies may be collected into more than one place.
        List<String> classesDirs = new ArrayList<>(), jarsDirs = new ArrayList<>();
        String outDir = "out";

        for (int i = 0; i < args.length; i++) {
            switch (args[i]) {
                case "--classes": classesDirs.add(args[++i]); break;
                case "--jars":    jarsDirs.add(args[++i]); break;
                case "--out":     outDir = args[++i]; break;
                default:
                    System.err.println("unknown arg: " + args[i]);
                    System.exit(2);
            }
        }

        Files.createDirectories(Paths.get(outDir));
        classesOut     = writer(outDir + "/classes.csv");
        methodsOut     = writer(outDir + "/methods.csv");
        callsOut       = writer(outDir + "/calls.csv");
        extendsOut     = writer(outDir + "/extends.csv");
        implementsOut  = writer(outDir + "/implements.csv");
        beansOut       = writer(outDir + "/beans.csv");
        beanMethodsOut = writer(outDir + "/bean_methods.csv");
        injectionsOut  = writer(outDir + "/injections.csv");

        classesOut.println("fqn,name,package,is_interface,is_abstract,is_external,source_file,artifact");
        methodsOut.println("fqn,owner,name,descriptor,is_static,is_abstract,is_external,line,line_end,artifact");
        callsOut.println("src,dst,dst_owner,opcode,exact,line");
        extendsOut.println("child,parent");
        implementsOut.println("child,iface");
        beansOut.println("class_fqn,stereotype");
        beanMethodsOut.println("method_fqn,owner,return_type");
        injectionsOut.println("owner_class,member_kind,member_name,param_index,declared_type,qualifier");

        // Application classes first, so appClasses is complete before any JAR is read.
        for (String d : classesDirs) {
            scanClassesDir(Paths.get(d));
        }
        for (String d : jarsDirs) {
            scanJarsDir(Paths.get(d));
        }

        for (PrintWriter w : new PrintWriter[]{classesOut, methodsOut, callsOut, extendsOut, implementsOut,
                beansOut, beanMethodsOut, injectionsOut}) {
            w.flush();
            w.close();
        }

        // Resolution statistics: how many call sites point at a class we actually indexed.
        int internal = 0;
        for (String owner : callTargets) {
            if (knownClasses.contains(owner)) internal++;
        }
        int external = callTargets.size() - internal;

        StringBuilder stats = new StringBuilder();
        stats.append("{\n");
        stats.append("  \"classes\": ").append(classCount).append(",\n");
        stats.append("  \"methods\": ").append(methodCount).append(",\n");
        stats.append("  \"call_sites\": ").append(callCount).append(",\n");
        stats.append("  \"exact_calls\": ").append(exactCalls).append(",\n");
        stats.append("  \"virtual_calls\": ").append(virtualCalls).append(",\n");
        stats.append("  \"invokedynamic_sites\": ").append(indyCalls).append(",\n");
        stats.append("  \"invokedynamic_resolved\": ").append(indyResolved).append(",\n");
        stats.append("  \"targets_inside_index\": ").append(internal).append(",\n");
        stats.append("  \"targets_outside_index\": ").append(external).append(",\n");
        stats.append("  \"spring_beans\": ").append(beanCount).append(",\n");
        stats.append("  \"spring_bean_methods\": ").append(beanMethodCount).append(",\n");
        stats.append("  \"spring_injection_points\": ").append(injectionCount).append(",\n");
        stats.append("  \"jar_classes_shadowed_by_app\": ").append(shadowedJarClasses).append("\n");
        stats.append("}\n");

        Files.write(Paths.get(outDir, "extract_stats.json"), stats.toString().getBytes("UTF-8"));
        System.out.print(stats);
    }

    // ---- input scanning --------------------------------------------------

    static void scanClassesDir(Path root) throws IOException {
        if (!Files.isDirectory(root)) {
            System.err.println("classes dir not found: " + root);
            return;
        }
        try (var walk = Files.walk(root)) {
            for (Path p : (Iterable<Path>) walk.filter(f -> f.toString().endsWith(".class"))::iterator) {
                try (InputStream in = Files.newInputStream(p)) {
                    visit(in, false, "app");
                } catch (Exception e) {
                    System.err.println("skip " + p + ": " + e);
                }
            }
        }
    }

    static void scanJarsDir(Path root) throws IOException {
        if (!Files.isDirectory(root)) {
            System.err.println("jars dir not found: " + root);
            return;
        }
        try (var list = Files.list(root)) {
            for (Path jar : (Iterable<Path>) list.filter(f -> f.toString().endsWith(".jar"))::iterator) {
                String artifact = jar.getFileName().toString();
                try (JarFile jf = new JarFile(jar.toFile())) {
                    Enumeration<JarEntry> en = jf.entries();
                    while (en.hasMoreElements()) {
                        JarEntry e = en.nextElement();
                        if (!e.getName().endsWith(".class")) continue;
                        try (InputStream in = jf.getInputStream(e)) {
                            visit(in, true, artifact);
                        } catch (Exception ex) {
                            System.err.println("skip " + artifact + "!" + e.getName() + ": " + ex);
                        }
                    }
                } catch (Exception ex) {
                    System.err.println("skip jar " + jar + ": " + ex);
                }
            }
        }
    }

    static void visit(InputStream in, boolean external, String artifact) throws IOException {
        ClassReader reader = new ClassReader(in);
        if (!external) {
            appClasses.add(reader.getClassName());
        } else if (appClasses.contains(reader.getClassName())) {
            shadowedJarClasses++;
            return;
        }
        reader.accept(new GraphClassVisitor(external, artifact), ClassReader.SKIP_FRAMES);
    }

    // ---- ASM visitors ----------------------------------------------------

    static class GraphClassVisitor extends ClassVisitor {
        final boolean external;
        final String artifact;
        String internalName, fqn, simple, pkg;
        String sourceFile = "";
        boolean isInterface, isAbstract;

        // -- Spring DI (phase 10) --
        String stereotype = null;
        /** Every constructor with 1+ parameters, keyed by descriptor -- only known
         *  in full once every constructor has been visited, so the "is this class's
         *  sole constructor implicitly autowired" decision waits for visitEnd(). */
        final Map<String, Type[]> ctorParams = new LinkedHashMap<>();
        /** Set if some constructor carries an explicit @Autowired -- that one wins
         *  outright, regardless of how many other constructors exist. */
        String explicitAutowiredCtorDesc = null;

        GraphClassVisitor(boolean external, String artifact) {
            super(Opcodes.ASM9);
            this.external = external;
            this.artifact = artifact;
        }

        @Override
        public void visit(int version, int access, String name, String signature,
                          String superName, String[] interfaces) {
            internalName = name;
            fqn = dotted(name);
            knownClasses.add(name);
            classCount++;

            isInterface = (access & Opcodes.ACC_INTERFACE) != 0;
            isAbstract = (access & Opcodes.ACC_ABSTRACT) != 0;

            int lastSlash = name.lastIndexOf('/');
            pkg = lastSlash < 0 ? "" : dotted(name.substring(0, lastSlash));
            simple = lastSlash < 0 ? name : name.substring(lastSlash + 1);

            // superName is null only for java/lang/Object
            if (superName != null && !superName.equals("java/lang/Object")) {
                row(extendsOut, fqn, dotted(superName));
            }
            if (interfaces != null) {
                for (String i : interfaces) {
                    row(implementsOut, fqn, dotted(i));
                }
            }
        }

        @Override
        public void visitSource(String source, String debug) {
            if (source != null) sourceFile = source;
        }

        @Override
        public AnnotationVisitor visitAnnotation(String descriptor, boolean visible) {
            String st = STEREOTYPES.get(descriptor);
            if (st != null) stereotype = st;
            return null;
        }

        @Override
        public FieldVisitor visitField(int access, String name, String descriptor,
                                       String signature, Object value) {
            return new GraphFieldVisitor(fqn, name, descriptor);
        }

        @Override
        public MethodVisitor visitMethod(int access, String name, String descriptor,
                                         String signature, String[] exceptions) {
            String methodFqn = fqn + "#" + name + descriptor;
            methodCount++;

            boolean isStatic = (access & Opcodes.ACC_STATIC) != 0;
            boolean mAbstract = (access & Opcodes.ACC_ABSTRACT) != 0;

            // Param types are known directly from the descriptor -- no need to wait
            // for anything else about this constructor to register it as a candidate.
            if (name.equals("<init>")) {
                Type[] params = Type.getArgumentTypes(descriptor);
                if (params.length > 0) ctorParams.put(descriptor, params);
            }

            return new GraphMethodVisitor(methodFqn, fqn, name, descriptor,
                    isStatic, mAbstract, external, artifact, this);
        }

        void onBeanMethod(String methodFqn, String returnTypeFqn) {
            row(beanMethodsOut, methodFqn, fqn, returnTypeFqn);
            beanMethodCount++;
        }

        // ASM emits visitSource() after visit(), so the class row is written here,
        // once every attribute of the class is known.
        @Override
        public void visitEnd() {
            row(classesOut, fqn, simple, pkg,
                    String.valueOf(isInterface), String.valueOf(isAbstract),
                    String.valueOf(external), sourceFile, artifact);

            if (stereotype != null) {
                row(beansOut, fqn, stereotype);
                beanCount++;
            }

            // Implicit constructor injection: Spring auto-wires a class's sole
            // constructor with no annotation needed at all (the common modern idiom,
            // and the only pattern PetClinic itself uses). An explicit @Autowired
            // constructor overrides the heuristic outright; with neither an explicit
            // mark nor exactly one parameterized constructor, which one Spring would
            // pick is genuinely ambiguous from bytecode alone, so none is emitted --
            // reported as unresolved by omission, not guessed.
            //
            // Gated on `stereotype != null`: Spring only ever calls this constructor
            // itself for a class it manages as a bean. Without this gate the heuristic
            // fires for every ordinary class in the 53k-class index that happens to
            // have exactly one constructor -- true of huge numbers of unrelated DTOs,
            // exceptions and library classes -- which is a real bug this project hit
            // and fixed during development, not a hypothetical one.
            String injCtorDesc = stereotype == null ? null
                    : explicitAutowiredCtorDesc != null
                        ? explicitAutowiredCtorDesc
                        : (ctorParams.size() == 1 ? ctorParams.keySet().iterator().next() : null);
            if (injCtorDesc != null) {
                Type[] params = ctorParams.get(injCtorDesc);
                for (int i = 0; i < params.length; i++) {
                    if (params[i].getSort() != Type.OBJECT) continue; // skip primitives/arrays
                    row(injectionsOut, fqn, "constructor", "<init>", String.valueOf(i),
                            dotted(params[i].getInternalName()), "");
                    injectionCount++;
                }
            }
        }
    }

    /** Field-level @Autowired (with optional @Qualifier) -- the explicit-annotation
     *  injection path, unlike the implicit constructor case above. */
    static class GraphFieldVisitor extends FieldVisitor {
        final String ownerFqn, name, descriptor;
        boolean autowired = false;
        String qualifier = "";

        GraphFieldVisitor(String ownerFqn, String name, String descriptor) {
            super(Opcodes.ASM9);
            this.ownerFqn = ownerFqn;
            this.name = name;
            this.descriptor = descriptor;
        }

        @Override
        public AnnotationVisitor visitAnnotation(String descriptor, boolean visible) {
            if (descriptor.equals(AUTOWIRED_DESC)) {
                autowired = true;
                return null;
            }
            if (descriptor.equals(QUALIFIER_DESC)) {
                return new AnnotationVisitor(Opcodes.ASM9) {
                    @Override
                    public void visit(String name, Object value) {
                        if ("value".equals(name) && value instanceof String) {
                            qualifier = (String) value;
                        }
                    }
                };
            }
            return null;
        }

        @Override
        public void visitEnd() {
            if (!autowired) return;
            Type t = Type.getType(descriptor);
            if (t.getSort() != Type.OBJECT) return;
            row(injectionsOut, ownerFqn, "field", name, "0", dotted(t.getInternalName()), qualifier);
            injectionCount++;
        }
    }

    static class GraphMethodVisitor extends MethodVisitor {
        final String srcFqn, owner, name, descriptor, artifact;
        final boolean isStatic, isAbstract, external;
        final GraphClassVisitor classVisitor;
        int line = -1;
        boolean lineRecorded = false;
        int firstLine = -1;
        int lastLine = -1;
        boolean isBeanMethod = false;

        GraphMethodVisitor(String srcFqn, String owner, String name, String descriptor,
                           boolean isStatic, boolean isAbstract, boolean external, String artifact,
                           GraphClassVisitor classVisitor) {
            super(Opcodes.ASM9);
            this.srcFqn = srcFqn;
            this.owner = owner;
            this.name = name;
            this.descriptor = descriptor;
            this.isStatic = isStatic;
            this.isAbstract = isAbstract;
            this.external = external;
            this.artifact = artifact;
            this.classVisitor = classVisitor;
        }

        @Override
        public AnnotationVisitor visitAnnotation(String descAnn, boolean visible) {
            if (descAnn.equals(BEAN_DESC)) {
                isBeanMethod = true;
            } else if (descAnn.equals(AUTOWIRED_DESC)) {
                if (name.equals("<init>")) {
                    // Explicit mark on a constructor wins outright over the
                    // "exactly one parameterized constructor" implicit heuristic.
                    classVisitor.explicitAutowiredCtorDesc = descriptor;
                } else {
                    // Setter injection: always explicit in modern Spring, so (unlike
                    // the constructor case) this needs no class-level disambiguation
                    // -- a single-parameter @Autowired method is unconditionally an
                    // injection point. General multi-parameter @Autowired methods
                    // (rare, and absent from PetClinic) are out of scope.
                    Type[] params = Type.getArgumentTypes(descriptor);
                    if (params.length == 1 && params[0].getSort() == Type.OBJECT) {
                        row(injectionsOut, owner, "setter", name, "0",
                                dotted(params[0].getInternalName()), "");
                        injectionCount++;
                    }
                }
            }
            return null;
        }

        // The method row is written at visitEnd() so it can carry the declaration line,
        // which is only known once the first LineNumberTable entry has been seen.
        @Override
        public void visitEnd() {
            row(methodsOut, srcFqn, owner, name, descriptor,
                    String.valueOf(isStatic), String.valueOf(isAbstract),
                    String.valueOf(external),
                    firstLine >= 0 ? String.valueOf(firstLine) : "",
                    lastLine >= 0 ? String.valueOf(lastLine) : "", artifact);

            if (isBeanMethod) {
                Type ret = Type.getReturnType(descriptor);
                if (ret.getSort() == Type.OBJECT) {
                    classVisitor.onBeanMethod(srcFqn, dotted(ret.getInternalName()));
                }
            }
        }

        @Override
        public void visitLineNumber(int lineNo, Label start) {
            line = lineNo;
            lineRecorded = true;
            if (firstLine < 0 || lineNo < firstLine) firstLine = lineNo;
            if (lineNo > lastLine) lastLine = lineNo;
        }

        @Override
        public void visitMethodInsn(int opcode, String owner, String name,
                                    String descriptor, boolean isInterface) {
            // INVOKESTATIC and INVOKESPECIAL (constructors, super calls, private methods)
            // are statically bound, so the target is provable. INVOKEVIRTUAL and
            // INVOKEINTERFACE name only the *declared* type; the runtime target depends
            // on dispatch, so these are marked inexact and expanded later by CHA.
            boolean exact = opcode == Opcodes.INVOKESTATIC || opcode == Opcodes.INVOKESPECIAL;
            if (exact) exactCalls++; else virtualCalls++;

            emitCall(owner, name, descriptor, opcodeName(opcode), exact);
        }

        @Override
        public void visitInvokeDynamicInsn(String name, String descriptor,
                                           Handle bootstrapMethodHandle,
                                           Object... bootstrapMethodArguments) {
            indyCalls++;
            // Lambdas and method references compile to invokedynamic. LambdaMetafactory
            // passes the actual implementation method as a Handle in the bootstrap
            // arguments, so the lambda body is usually recoverable.
            if (bootstrapMethodArguments != null) {
                for (Object arg : bootstrapMethodArguments) {
                    if (arg instanceof Handle) {
                        Handle h = (Handle) arg;
                        indyResolved++;
                        emitCall(h.getOwner(), h.getName(), h.getDesc(), "INVOKEDYNAMIC", false);
                    }
                }
            }
        }

        void emitCall(String owner, String name, String descriptor, String opcode, boolean exact) {
            // Array types can appear as an owner (e.g. clone() on int[]); they have no
            // class node, so skip them rather than emitting a dangling edge.
            if (owner.startsWith("[")) return;

            String dst = dotted(owner) + "#" + name + descriptor;
            callCount++;
            callTargets.add(owner);
            row(callsOut, srcFqn, dst, dotted(owner), opcode,
                    String.valueOf(exact), lineRecorded ? String.valueOf(line) : "");
        }
    }

    // ---- helpers ---------------------------------------------------------

    static String dotted(String internalName) {
        return internalName.replace('/', '.');
    }

    static String opcodeName(int opcode) {
        switch (opcode) {
            case Opcodes.INVOKEVIRTUAL:   return "INVOKEVIRTUAL";
            case Opcodes.INVOKESTATIC:    return "INVOKESTATIC";
            case Opcodes.INVOKESPECIAL:   return "INVOKESPECIAL";
            case Opcodes.INVOKEINTERFACE: return "INVOKEINTERFACE";
            default:                      return "OP" + opcode;
        }
    }

    static PrintWriter writer(String path) throws IOException {
        return new PrintWriter(new BufferedWriter(new OutputStreamWriter(
                new FileOutputStream(path), "UTF-8"), 1 << 16));
    }

    /** Writes a CSV row, quoting every field so descriptors and signatures stay intact. */
    static synchronized void row(PrintWriter out, String... fields) {
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < fields.length; i++) {
            if (i > 0) sb.append(',');
            String f = fields[i] == null ? "" : fields[i];
            sb.append('"').append(f.replace("\"", "\"\"")).append('"');
        }
        out.println(sb);
    }
}
