// Receive-only stdin adapter for the pinned MIT-licensed sstv decoder.
// No device, network, filesystem, radio-control or encoding path is exposed.
use std::io::{self, BufReader, BufWriter, Read, Write};
use sstv::{modes::ROBOT_36, Decoder};

struct Samples<R: Read> {
    reader: R,
    failed: bool,
}

impl<R: Read> Iterator for Samples<R> {
    type Item = i16;

    fn next(&mut self) -> Option<i16> {
        let mut bytes = [0u8; 2];
        loop {
            match self.reader.read(&mut bytes[..1]) {
                Ok(0) => return None,
                Ok(_) => break,
                Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
                Err(_) => { self.failed = true; return None; }
            }
        }
        if self.reader.read_exact(&mut bytes[1..]).is_err() {
            self.failed = true;
            return None;
        }
        Some(i16::from_le_bytes(bytes))
    }
}

fn receive() -> io::Result<()> {
    if std::env::args_os().len() != 1 {
        return Err(io::Error::new(io::ErrorKind::InvalidInput, "This decoder takes stdin only, without arguments."));
    }
    let input = io::stdin();
    let output = io::stdout();
    let mut samples = Samples { reader: BufReader::with_capacity(4096, input.lock()), failed: false };
    let mut output = BufWriter::with_capacity(4096, output.lock());
    loop {
        // The forced mode accepts its VIS header or three correctly spaced
        // Robot 36 line sync pulses. Headers for other modes are ignored.
        let Some(mut rows) = Decoder::new(&mut samples, 48_000).with_mode(ROBOT_36).rows() else { break; };
        writeln!(output, "SSTV1 {{\"event\":\"begin\",\"mode\":\"Robot36\",\"width\":320,\"height\":240}}")?;
        output.flush()?;
        let mut count = 0usize;
        for row in rows.by_ref() {
            // This fixed mode must never produce another geometry.
            if row.index() != count || count >= 240 || row.pixels().len() != 320 {
                return Err(io::Error::new(io::ErrorKind::InvalidData, "Unexpected Robot 36 geometry."));
            }
            write!(output, "SSTV1 {{\"event\":\"row\",\"row\":{},\"rgb\":\"", count)?;
            for pixel in row.pixels() {
                write!(output, "{:02x}{:02x}{:02x}", pixel.red(), pixel.green(), pixel.blue())?;
            }
            writeln!(output, "\"}}")?;
            output.flush()?;
            count += 1;
        }
        writeln!(output, "SSTV1 {{\"event\":\"end\",\"complete\":{}}}", count == 240)?;
        output.flush()?;
    }
    if samples.failed {
        return Err(io::Error::new(io::ErrorKind::InvalidData, "Incomplete or interrupted PCM input."));
    }
    Ok(())
}

fn main() {
    if receive().is_err() {
        eprintln!("The receive-only SSTV decoder could not complete its input/output stream.");
        std::process::exit(1);
    }
}
